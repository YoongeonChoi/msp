from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.broker.toss_auth import TOSS_OPENAPI_BASE_URL, TossAuth
from app.adapters.broker.toss_models import (
    TossAccount,
    TossApiErrorEnvelope,
    TossApiResponse,
    TossCandlePage,
    TossCandleQuery,
    TossHoldingsOverview,
    TossOrder,
    TossOrderListQuery,
    TossOrderPage,
    TossPriceResponse,
)
from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
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

ParsedModel = TypeVar("ParsedModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TossGetRequest:
    path: str
    params: Mapping[str, str | int | bool] | None = None
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

    async def provider_health(self) -> bool:
        try:
            await self.list_accounts()
        except ProviderError:
            return False
        return True

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
        envelope = await self._get_model(
            TossGetRequest(
                "/api/v1/holdings",
                params=query_params,
                account_seq=self._resolve_account_seq(account_seq),
            ),
            TossApiResponse[TossHoldingsOverview],
        )
        return envelope.result

    async def get_prices(self, symbols: Sequence[str]) -> list[TossPriceResponse]:
        if not symbols:
            raise ProviderSchemaError("toss", "toss_prices_symbols_empty")
        envelope = await self._get_model(
            TossGetRequest("/api/v1/prices", params={"symbols": ",".join(symbols)}),
            TossApiResponse[list[TossPriceResponse]],
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
        envelope = await self._get_model(
            TossGetRequest(
                "/api/v1/orders",
                params=params,
                account_seq=self._resolve_account_seq(query.account_seq),
            ),
            TossApiResponse[TossOrderPage],
        )
        return envelope.result

    async def get_order(self, order_id: str, account_seq: int | None = None) -> TossOrder:
        envelope = await self._get_model(
            TossGetRequest(
                f"/api/v1/orders/{order_id}",
                account_seq=self._resolve_account_seq(account_seq),
            ),
            TossApiResponse[TossOrder],
        )
        return envelope.result

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise ProviderUnavailableError("toss", "toss_live_order_endpoint_not_implemented")

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

    async def _headers(self, account_seq: int | None = None) -> dict[str, str]:
        token = await self.auth.access_token()
        headers = {"authorization": f"Bearer {token}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        return headers

    def _resolve_account_seq(self, account_seq: int | None) -> int:
        if account_seq is not None:
            return account_seq
        if self.settings.toss_account_id is None:
            raise ProviderAuthError("toss", "toss_account_seq_missing")
        raw_value = self.settings.toss_account_id.get_secret_value()
        try:
            return int(raw_value)
        except ValueError as exc:
            raise ProviderAuthError("toss", "toss_account_seq_invalid") from exc


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
