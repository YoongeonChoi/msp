from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.broker.toss_auth import TossAuth
from app.adapters.broker.toss_client import TossClient
from app.adapters.broker.toss_models import TossCandleQuery
from app.application.ports.broker_port import BrokerOrderRequest
from app.config import Settings
from app.domain.common.errors import ProviderRateLimitError, ProviderUnavailableError
from app.domain.common.json import JsonObject
from app.tools.test_toss_readonly import _mask_identifier


async def test_toss_auth_uses_client_credentials_form_token_flow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        assert request.method == "POST"
        assert request.url.path == "/oauth2/token"
        assert form["grant_type"] == ["client_credentials"]
        assert form["client_id"] == ["client-id"]
        assert form["client_secret"] == ["client-secret"]
        return httpx.Response(
            200,
            json={"access_token": "token-value", "token_type": "Bearer", "expires_in": 3600},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=client)

    token = await auth.access_token()

    assert token == "token-value"
    assert len(requests) == 1
    await client.aclose()


async def test_toss_client_parses_account_response() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/accounts": {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 1,
                        "accountType": "BROKERAGE",
                    }
                ]
            }
        }
    )

    accounts = await client.list_accounts()

    assert accounts[0].account_no == "12345678901"
    assert accounts[0].account_seq == 1
    assert requests[-1].headers["authorization"] == "Bearer token-value"


async def test_toss_client_parses_position_response() -> None:
    client, requests = _client_with_responses({"/api/v1/holdings": _holdings_payload()})

    holdings = await client.get_holdings(account_seq=1)

    assert holdings.items[0].symbol == "005930"
    assert holdings.items[0].quantity == 100
    assert requests[-1].headers["x-tossinvest-account"] == "1"


async def test_toss_client_parses_price_and_candle_responses() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/prices": {
                "result": [
                    {
                        "symbol": "005930",
                        "timestamp": "2026-03-25T09:30:00.123+09:00",
                        "lastPrice": "72000",
                        "currency": "KRW",
                    }
                ]
            },
            "/api/v1/candles": {
                "result": {
                    "candles": [
                        {
                            "timestamp": "2026-03-25T09:00:00+09:00",
                            "openPrice": "71600",
                            "highPrice": "72300",
                            "lowPrice": "71500",
                            "closePrice": "72000",
                            "volume": "3521000",
                            "currency": "KRW",
                        }
                    ],
                    "nextBefore": None,
                }
            },
        }
    )

    prices = await client.get_prices(["005930"])
    candles = await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=1))

    assert prices[0].last_price == 72000
    assert candles.candles[0].close_price == 72000
    assert {request.url.path for request in requests} >= {"/api/v1/prices", "/api/v1/candles"}


async def test_toss_provider_error_mapping_uses_safe_error_code() -> None:
    client, _requests = _client_with_responses(
        {
            "/api/v1/accounts": (
                429,
                {
                    "error": {
                        "requestId": "request-id",
                        "code": "rate-limit-exceeded",
                        "message": "too many requests",
                    }
                },
            )
        }
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await client.list_accounts()

    assert exc_info.value.safe_message == "toss_rate-limit-exceeded"


async def test_toss_place_order_remains_disabled_and_does_not_call_order_endpoint() -> None:
    client, requests = _client_with_responses({})
    request = BrokerOrderRequest(
        symbol="005930",
        side="buy",
        amount_krw=100000,
        idempotency_key="paper-only",
    )

    with pytest.raises(ProviderUnavailableError):
        await client.place_order(request)

    assert all(
        http_request.method != "POST" or http_request.url.path != "/api/v1/orders"
        for http_request in requests
    )


def test_toss_readonly_command_masks_account_identifiers() -> None:
    masked = _mask_identifier("12345678901")

    assert masked == "12***01"
    assert "12345678901" not in masked


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "MOCK_PROVIDERS": False,
            "TOSS_CLIENT_ID": SecretStr("client-id"),
            "TOSS_CLIENT_SECRET": SecretStr("client-secret"),
            "TOSS_ACCOUNT_ID": SecretStr("1"),
        }
    )


def _client_with_responses(
    responses: dict[str, JsonObject | tuple[int, JsonObject]],
) -> tuple[TossClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "token-value", "token_type": "Bearer", "expires_in": 3600},
            )
        payload = responses[request.url.path]
        if isinstance(payload, tuple):
            return httpx.Response(payload[0], json=payload[1], request=request)
        return httpx.Response(200, json=payload, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=http_client)
    return TossClient(_settings(), auth=auth, client=http_client), requests


def _holdings_payload() -> JsonObject:
    return {
        "result": {
            "totalPurchaseAmount": {"krw": "6500000", "usd": None},
            "marketValue": {
                "amount": {"krw": "7200000", "usd": None},
                "amountAfterCost": {"krw": "7050000", "usd": None},
            },
            "profitLoss": {
                "amount": {"krw": "700000", "usd": None},
                "amountAfterCost": {"krw": "550000", "usd": None},
                "rate": "0.1077",
                "rateAfterCost": "0.0846",
            },
            "dailyProfitLoss": {
                "amount": {"krw": "100000", "usd": None},
                "rate": "0.0141",
            },
            "items": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "100",
                    "lastPrice": "72000",
                    "averagePurchasePrice": "65000",
                    "marketValue": {
                        "purchaseAmount": "6500000",
                        "amount": "7200000",
                        "amountAfterCost": "7050000",
                    },
                    "profitLoss": {
                        "amount": "700000",
                        "amountAfterCost": "550000",
                        "rate": "0.1077",
                        "rateAfterCost": "0.0846",
                    },
                    "dailyProfitLoss": {"amount": "100000", "rate": "0.0141"},
                    "cost": {"commission": "14400", "tax": "135600"},
                }
            ],
        }
    }
