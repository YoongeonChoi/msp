from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.broker.toss_auth import TOSS_OPENAPI_BASE_URL, TossAuth
from app.adapters.broker.toss_models import (
    TossAccount,
    TossApiErrorEnvelope,
    TossApiResponse,
    TossBuyingPowerResponse,
    TossCandlePage,
    TossCandleQuery,
    TossHoldingsOverview,
    TossKrMarketCalendarResponse,
    TossOrder,
    TossOrderCreateResult,
    TossOrderListQuery,
    TossOrderOperationResult,
    TossOrderPage,
    TossPriceResponse,
)
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderReconciliationStatus,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.config import Settings
from app.domain.common.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.portfolio.entities import Position
from app.domain.trading.entities import AccountState

ParsedModel = TypeVar("ParsedModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TossGetRequest:
    path: str
    params: Mapping[str, str | int | bool] | None = None
    account_seq: int | None = None


@dataclass(frozen=True, slots=True)
class TossPostRequest:
    path: str
    json: Mapping[str, str | int | bool]
    account_seq: int | None = None


class TossClient:
    def __init__(
        self,
        settings: Settings,
        auth: TossAuth | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or TossAuth(settings)
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self.base_url = TOSS_OPENAPI_BASE_URL
        self._cached_account_seq: int | None = None
        self._provider_health_details: dict[str, object] = {}

    async def provider_health(self) -> bool:
        self._provider_health_details = {}
        try:
            await self.list_accounts()
            await self._resolve_account_seq(None)
        except ProviderError as exc:
            self._provider_health_details = {
                "error_type": type(exc).__name__,
                "reason": exc.safe_message,
            }
            return False
        return True

    def provider_health_details(self) -> dict[str, object]:
        return dict(self._provider_health_details)

    async def list_accounts(self) -> list[TossAccount]:
        envelope = await self._get_model(
            TossGetRequest("/api/v1/accounts"),
            TossApiResponse[list[TossAccount]],
        )
        return envelope.result

    async def get_holdings(
        self,
        account_seq: int | None = None,
        symbol: str | None = None,
    ) -> TossHoldingsOverview:
        query_params: dict[str, str | int | bool] | None = (
            {"symbol": symbol} if symbol is not None else None
        )
        resolved_account_seq = await self._resolve_account_seq(account_seq)
        envelope = await self._get_model(
            TossGetRequest(
                "/api/v1/holdings",
                params=query_params,
                account_seq=resolved_account_seq,
            ),
            TossApiResponse[TossHoldingsOverview],
        )
        return envelope.result

    async def get_buying_power(
        self,
        currency: str = "KRW",
        account_seq: int | None = None,
    ) -> TossBuyingPowerResponse:
        resolved_account_seq = await self._resolve_account_seq(account_seq)
        envelope = await self._get_model(
            TossGetRequest(
                "/api/v1/buying-power",
                params={"currency": currency},
                account_seq=resolved_account_seq,
            ),
            TossApiResponse[TossBuyingPowerResponse],
        )
        return envelope.result

    async def get_account_state(self, now: datetime) -> AccountState:
        buying_power = await self.get_buying_power("KRW")
        holdings = await self.get_holdings()
        if buying_power.currency != "KRW":
            raise ProviderSchemaError("toss", "toss_buying_power_currency_not_krw")
        cash_krw = _decimal_krw_to_int(buying_power.cash_buying_power)
        holdings_krw = _decimal_krw_to_int(holdings.market_value.amount_after_cost.krw)
        return AccountState(
            synced=True,
            cash_krw=cash_krw,
            equity_krw=cash_krw + holdings_krw,
            daily_loss_pct=_daily_loss_pct(holdings),
            daily_order_count=0,
            synced_at=now,
            daily_order_count_verified=False,
        )

    async def get_positions(self, now: datetime) -> list[Position]:
        holdings = await self.get_holdings()
        positions: list[Position] = []
        for item in holdings.items:
            if (
                item.market_country != "KR"
                or item.currency != "KRW"
                or _kr_symbol(item.symbol) is None
            ):
                continue
            positions.append(
                Position(
                    symbol=item.symbol,
                    quantity=_decimal_integral_to_int(
                        item.quantity,
                        "toss_position_quantity_invalid",
                    ),
                    avg_price_krw=_decimal_krw_to_int(item.average_purchase_price),
                    current_price_krw=_decimal_krw_to_int(item.last_price),
                    sector="unknown",
                )
            )
        return positions

    async def get_prices(self, symbols: Sequence[str]) -> list[TossPriceResponse]:
        if not symbols:
            raise ProviderSchemaError("toss", "toss_prices_symbols_empty")
        envelope = await self._get_model(
            TossGetRequest("/api/v1/prices", params={"symbols": ",".join(symbols)}),
            TossApiResponse[list[TossPriceResponse]],
        )
        return envelope.result

    async def get_kr_market_calendar(
        self,
        target_date: date | None = None,
    ) -> TossKrMarketCalendarResponse:
        params = {"date": target_date.isoformat()} if target_date is not None else None
        envelope = await self._get_model(
            TossGetRequest("/api/v1/market-calendar/KR", params=params),
            TossApiResponse[TossKrMarketCalendarResponse],
        )
        return envelope.result

    async def get_candles(self, query: TossCandleQuery) -> TossCandlePage:
        params: dict[str, str | int | bool] = {
            "symbol": query.symbol,
            "interval": query.interval,
            "count": query.count,
            "adjusted": query.adjusted,
        }
        if query.before is not None:
            params["before"] = query.before.isoformat()
        envelope = await self._get_model(
            TossGetRequest("/api/v1/candles", params=params),
            TossApiResponse[TossCandlePage],
        )
        return envelope.result

    async def list_orders(self, query: TossOrderListQuery) -> TossOrderPage:
        params: dict[str, str | int] = {"status": query.status.value}
        if query.symbol is not None:
            params["symbol"] = query.symbol
        if query.from_date is not None:
            params["from"] = query.from_date.isoformat()
        if query.to_date is not None:
            params["to"] = query.to_date.isoformat()
        if query.cursor is not None:
            params["cursor"] = query.cursor
        if query.limit is not None:
            params["limit"] = query.limit
        resolved_account_seq = await self._resolve_account_seq(query.account_seq)
        envelope = await self._get_model(
            TossGetRequest(
                "/api/v1/orders",
                params=params,
                account_seq=resolved_account_seq,
            ),
            TossApiResponse[TossOrderPage],
        )
        return envelope.result

    async def get_order(self, order_id: str, account_seq: int | None = None) -> TossOrder:
        resolved_account_seq = await self._resolve_account_seq(account_seq)
        envelope = await self._get_model(
            TossGetRequest(
                f"/api/v1/orders/{order_id}",
                account_seq=resolved_account_seq,
            ),
            TossApiResponse[TossOrder],
        )
        return envelope.result

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        order = await self.get_order(provider_order_id)
        status = _map_toss_order_status(order.status)
        reason = (
            None
            if status != "unknown_requires_manual_check"
            else f"toss_order_status_{order.status}"
        )
        return BrokerOrderStatusResult(
            provider_order_id=order.order_id,
            status=status,
            reason=reason,
            raw_summary={
                "order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side,
                "order_type": order.order_type,
                "status": order.status,
                "quantity": str(order.quantity),
                "filled_quantity": str(order.execution.filled_quantity),
                "average_filled_price": (
                    str(order.execution.average_filled_price)
                    if order.execution.average_filled_price is not None
                    else None
                ),
                "filled_amount": (
                    str(order.execution.filled_amount)
                    if order.execution.filled_amount is not None
                    else None
                ),
                "ordered_at": order.ordered_at.isoformat(),
                "canceled_at": order.canceled_at.isoformat() if order.canceled_at else None,
            },
        )

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        if not provider_order_id:
            raise ProviderSchemaError("toss", "toss_cancel_order_id_missing")
        envelope = await self._post_model(
            TossPostRequest(
                f"/api/v1/orders/{provider_order_id}/cancel",
                json={},
                account_seq=await self._resolve_account_seq(None),
            ),
            TossApiResponse[TossOrderOperationResult],
        )
        cancel_order_id = envelope.result.order_id
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id=cancel_order_id,
            raw_summary={
                "original_order_id": provider_order_id,
                "cancel_order_id": cancel_order_id,
            },
        )

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        _validate_live_order_request(request)
        envelope = await self._post_model(
            TossPostRequest(
                "/api/v1/orders",
                json={
                    "clientOrderId": request.idempotency_key,
                    "symbol": request.symbol,
                    "side": "BUY" if request.side == "buy" else "SELL",
                    "orderType": "LIMIT",
                    "quantity": str(request.quantity),
                    "price": str(request.limit_price_krw),
                    "confirmHighValueOrder": request.amount_krw >= 100_000_000,
                },
                account_seq=await self._resolve_account_seq(None),
            ),
            TossApiResponse[TossOrderCreateResult],
        )
        result = envelope.result
        return BrokerOrderResult(
            provider_order_id=result.order_id,
            status="sent",
            raw_summary={
                "order_id": result.order_id,
                "client_order_id": result.client_order_id,
                "symbol": request.symbol,
                "side": request.side,
                "order_type": "LIMIT",
                "quantity": request.quantity,
                "limit_price_krw": request.limit_price_krw,
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        await self.auth.aclose()

    async def _get_model(
        self,
        request: TossGetRequest,
        model_type: type[ParsedModel],
    ) -> ParsedModel:
        try:
            response = await self.client.get(
                f"{self.base_url}{request.path}",
                params=request.params,
                headers=await self._headers(request.account_seq),
            )
            _raise_for_toss_status(response)
            return model_type.model_validate_json(response.text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("toss", "toss_read_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise _provider_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("toss", "toss_read_request_failed") from exc
        except ValidationError as exc:
            raise ProviderSchemaError("toss", "toss_read_schema_invalid") from exc

    async def _post_model(
        self,
        request: TossPostRequest,
        model_type: type[ParsedModel],
    ) -> ParsedModel:
        try:
            response = await self.client.post(
                f"{self.base_url}{request.path}",
                json=request.json,
                headers=await self._headers(request.account_seq),
            )
            _raise_for_toss_status(response)
            return model_type.model_validate_json(response.text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("toss", "toss_write_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise _provider_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("toss", "toss_write_request_failed") from exc
        except ValidationError as exc:
            raise ProviderSchemaError("toss", "toss_write_schema_invalid") from exc

    async def _headers(self, account_seq: int | None = None) -> dict[str, str]:
        token = await self.auth.access_token()
        headers = {"authorization": f"Bearer {token}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        return headers

    async def _resolve_account_seq(self, account_seq: int | None) -> int:
        if account_seq is not None:
            return account_seq
        if self._cached_account_seq is not None:
            return self._cached_account_seq
        if self.settings.toss_account_id is None:
            accounts = await self.list_accounts()
            if len(accounts) == 1:
                self._cached_account_seq = accounts[0].account_seq
                return self._cached_account_seq
            if not accounts:
                raise ProviderAuthError("toss", "toss_account_seq_missing")
            raise ProviderAuthError("toss", "toss_account_seq_ambiguous")
        raw_value = self.settings.toss_account_id.get_secret_value()
        try:
            self._cached_account_seq = int(raw_value)
        except ValueError as exc:
            raise ProviderAuthError("toss", "toss_account_seq_invalid") from exc
        return self._cached_account_seq


def _raise_for_toss_status(response: httpx.Response) -> None:
    if response.is_error:
        response.raise_for_status()


def _provider_error_from_response(response: httpx.Response) -> ProviderError:
    safe_code = _safe_error_code(response)
    match response.status_code:
        case 400 | 401 | 403:
            return ProviderAuthError("toss", safe_code)
        case 429:
            return ProviderRateLimitError("toss", safe_code)
        case 500 | 502 | 503 | 504:
            return ProviderUnavailableError("toss", safe_code)
        case _:
            return ProviderUnknownError("toss", safe_code)


def _safe_error_code(response: httpx.Response) -> str:
    try:
        error_envelope = TossApiErrorEnvelope.model_validate_json(response.text)
    except ValidationError:
        return f"toss_http_{response.status_code}"
    return f"toss_{error_envelope.error.code}"


def _kr_symbol(value: str) -> str | None:
    if re.fullmatch(r"[0-9]{6}", value):
        return value
    return None


def _map_toss_order_status(status: str) -> BrokerOrderReconciliationStatus:
    match status:
        case "PENDING" | "PENDING_CANCEL" | "PENDING_REPLACE":
            return "sent"
        case "PARTIAL_FILLED":
            return "partial_filled"
        case "FILLED":
            return "filled"
        case "CANCELED":
            return "canceled"
        case "REJECTED":
            return "rejected"
        case _:
            return "unknown_requires_manual_check"


def _decimal_krw_to_int(value: Decimal) -> int:
    if value < 0 or value != value.to_integral_value():
        raise ProviderSchemaError("toss", "toss_krw_amount_not_nonnegative_integer")
    return int(value)


def _decimal_integral_to_int(value: Decimal, schema_code: str) -> int:
    if value < 0 or value != value.to_integral_value():
        raise ProviderSchemaError("toss", schema_code)
    return int(value)


def _daily_loss_pct(holdings: TossHoldingsOverview) -> float:
    daily_amount = holdings.daily_profit_loss.amount.krw
    daily_rate = holdings.daily_profit_loss.rate
    if daily_amount >= 0 and daily_rate >= 0:
        return 0.0
    return abs(float(daily_rate))


def _validate_live_order_request(request: BrokerOrderRequest) -> None:
    if not request.idempotency_key or len(request.idempotency_key) > 36:
        raise ProviderSchemaError("toss", "toss_client_order_id_invalid")
    if not all(
        character.isalnum() or character in {"-", "_"}
        for character in request.idempotency_key
    ):
        raise ProviderSchemaError("toss", "toss_client_order_id_invalid")
    if request.quantity <= 0:
        raise ProviderSchemaError("toss", "toss_order_quantity_invalid")
    if request.limit_price_krw <= 0:
        raise ProviderSchemaError("toss", "toss_order_price_invalid")
