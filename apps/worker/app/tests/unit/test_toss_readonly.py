from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.broker.toss_auth import TossAuth
from app.adapters.broker.toss_client import TossClient
from app.adapters.broker.toss_models import TossCandleQuery
from app.application.ports.broker_port import BrokerOrderRequest
from app.config import Settings
from app.domain.common.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderUnavailableError,
)
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


async def test_toss_auth_missing_credentials_fails_closed_without_http_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"unexpected": True}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        MOCK_PROVIDERS=False,
        TOSS_CLIENT_ID=None,
        TOSS_CLIENT_SECRET=None,
    )
    auth = TossAuth(settings, client=client)

    with pytest.raises(ProviderAuthError, match="toss_credentials_missing"):
        await auth.access_token()

    assert requests == []
    await client.aclose()


async def test_toss_client_missing_credentials_reports_unhealthy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"unexpected": True}, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        MOCK_PROVIDERS=False,
        TOSS_CLIENT_ID=None,
        TOSS_CLIENT_SECRET=None,
    )
    auth = TossAuth(settings, client=http_client)
    client = TossClient(settings, auth=auth, client=http_client)

    assert await client.provider_health() is False
    assert requests == []
    await http_client.aclose()


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


async def test_toss_client_infers_single_account_seq_when_not_configured() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/accounts": {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 7,
                        "accountType": "BROKERAGE",
                    }
                ]
            },
            "/api/v1/holdings": _holdings_payload(),
        },
        settings=_settings_without_account(),
    )

    await client.get_holdings()
    await client.get_holdings()

    assert [request.url.path for request in requests].count("/api/v1/accounts") == 1
    holdings_account_headers = [
        request.headers["x-tossinvest-account"]
        for request in requests
        if request.url.path == "/api/v1/holdings"
    ]
    assert holdings_account_headers == ["7", "7"]


async def test_toss_client_does_not_infer_account_seq_when_ambiguous() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/accounts": {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 7,
                        "accountType": "BROKERAGE",
                    },
                    {
                        "accountNo": "98765432109",
                        "accountSeq": 8,
                        "accountType": "BROKERAGE",
                    },
                ]
            },
            "/api/v1/holdings": _holdings_payload(),
        },
        settings=_settings_without_account(),
    )

    with pytest.raises(ProviderAuthError, match="toss_account_seq_ambiguous"):
        await client.get_holdings()

    assert [request.url.path for request in requests] == [
        "/oauth2/token",
        "/api/v1/accounts",
    ]


async def test_toss_client_health_fails_when_account_seq_is_ambiguous() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/accounts": {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 7,
                        "accountType": "BROKERAGE",
                    },
                    {
                        "accountNo": "98765432109",
                        "accountSeq": 8,
                        "accountType": "BROKERAGE",
                    },
                ]
            },
        },
        settings=_settings_without_account(),
    )

    assert await client.provider_health() is False
    assert [request.url.path for request in requests] == [
        "/oauth2/token",
        "/api/v1/accounts",
        "/api/v1/accounts",
    ]


async def test_toss_client_parses_position_response() -> None:
    client, requests = _client_with_responses({"/api/v1/holdings": _holdings_payload()})
    now = datetime(2026, 3, 25, 1, 0, tzinfo=UTC)

    holdings = await client.get_holdings(account_seq=1)
    positions = await client.get_positions(now)

    assert holdings.items[0].symbol == "005930"
    assert holdings.items[0].quantity == 100
    assert positions[0].symbol == "005930"
    assert positions[0].quantity == 100
    assert positions[0].avg_price_krw == 65_000
    assert positions[0].current_price_krw == 72_000
    account_headers = [
        request.headers["x-tossinvest-account"]
        for request in requests
        if request.url.path == "/api/v1/holdings"
        and "x-tossinvest-account" in request.headers
    ]
    assert account_headers == ["1", "1"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload["result"]["items"][0].__setitem__("marketCountry", "US"),
            "toss_position_scope_unsupported",
        ),
        (
            lambda payload: payload["result"]["items"][0].__setitem__("lastPrice", "0"),
            "toss_position_last_price_invalid",
        ),
        (
            lambda payload: payload["result"]["items"][0]["marketValue"].__setitem__(
                "amount", "7100000"
            ),
            "toss_position_market_value_mismatch",
        ),
        (
            lambda payload: payload["result"].__setitem__("items", []),
            "toss_positions_overview_mismatch",
        ),
    ],
)
async def test_toss_client_rejects_incomplete_or_unvalued_positions(
    mutate: Callable[[JsonObject], None],
    reason: str,
) -> None:
    payload = _holdings_payload()
    mutate(payload)
    client, _requests = _client_with_responses({"/api/v1/holdings": payload})

    with pytest.raises(ProviderSchemaError, match=reason):
        await client.get_positions(datetime(2026, 3, 25, 1, 0, tzinfo=UTC))


async def test_toss_client_parses_buying_power_calendar_and_account_state() -> None:
    client, requests = _client_with_responses(
        {
            "/api/v1/buying-power": {
                "result": {"currency": "KRW", "cashBuyingPower": "5000000"}
            },
            "/api/v1/holdings": _holdings_payload(),
            "/api/v1/market-calendar/KR": _kr_market_calendar_payload(),
        }
    )
    now = datetime(2026, 3, 25, 1, 0, tzinfo=UTC)

    buying_power = await client.get_buying_power()
    calendar = await client.get_kr_market_calendar(date(2026, 3, 25))
    account_state = await client.get_account_state(now)

    assert buying_power.cash_buying_power == 5_000_000
    assert calendar.today.integrated is not None
    assert calendar.today.integrated.regular_market is not None
    assert calendar.today.integrated.regular_market.start_time.hour == 9
    assert account_state.cash_krw == 5_000_000
    assert account_state.equity_krw == 12_050_000
    assert account_state.daily_loss_pct == 0.0
    assert account_state.daily_order_count == 0
    assert account_state.daily_order_count_verified is False
    account_paths = [
        request.url.path
        for request in requests
        if "x-tossinvest-account" in request.headers
    ]
    assert account_paths == [
        "/api/v1/buying-power",
        "/api/v1/buying-power",
        "/api/v1/holdings",
    ]


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


async def test_toss_place_order_is_quarantined_without_network_call() -> None:
    client, requests = _client_with_responses({})
    request = BrokerOrderRequest(
        symbol="005930",
        side="buy",
        amount_krw=75_000,
        idempotency_key="live-key-1",
        quantity=1,
        limit_price_krw=75_000,
    )

    with pytest.raises(ProviderUnavailableError, match="production_live_order_is_quarantined"):
        await client.place_order(request)
    assert requests == []


async def test_toss_order_status_is_quarantined_without_network_call() -> None:
    client, requests = _client_with_responses({})

    with pytest.raises(
        ProviderUnavailableError,
        match="production_order_status_network_is_quarantined",
    ):
        await client.get_order_status("order-1")
    assert requests == []


async def test_toss_order_listing_and_detail_are_quarantined_without_network_call() -> None:
    client, requests = _client_with_responses({})

    with pytest.raises(
        ProviderUnavailableError,
        match="production_order_status_network_is_quarantined",
    ):
        await client.get_order("order-1")
    assert requests == []


async def test_toss_cancel_order_is_quarantined_without_network_call() -> None:
    client, requests = _client_with_responses({})

    with pytest.raises(ProviderUnavailableError, match="production_live_cancel_is_quarantined"):
        await client.cancel_order("order-1")
    assert requests == []


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
            "TOSS_CREDENTIAL_SCOPE": "read_only",
            "TOSS_ORDER_CAPABLE_CREDENTIALS": False,
        }
    )


def _settings_without_account() -> Settings:
    return Settings.model_validate(
        {
            "MOCK_PROVIDERS": False,
            "TOSS_CLIENT_ID": SecretStr("client-id"),
            "TOSS_CLIENT_SECRET": SecretStr("client-secret"),
            "TOSS_ACCOUNT_ID": None,
            "TOSS_CREDENTIAL_SCOPE": "read_only",
            "TOSS_ORDER_CAPABLE_CREDENTIALS": False,
        }
    )


def _client_with_responses(
    responses: dict[str, JsonObject | tuple[int, JsonObject]],
    *,
    settings: Settings | None = None,
) -> tuple[TossClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    resolved_settings = settings or _settings()

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
    auth = TossAuth(resolved_settings, client=http_client)
    return TossClient(resolved_settings, auth=auth, client=http_client), requests


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


def _kr_market_calendar_payload() -> JsonObject:
    return {
        "result": {
            "today": {
                "date": "2026-03-25",
                "integrated": {
                    "regularMarket": {
                        "startTime": "2026-03-25T09:00:00+09:00",
                        "singlePriceAuctionStartTime": "2026-03-25T15:20:00+09:00",
                        "endTime": "2026-03-25T15:30:00+09:00",
                    }
                },
            },
            "previousBusinessDay": {
                "date": "2026-03-24",
                "integrated": None,
            },
            "nextBusinessDay": {
                "date": "2026-03-26",
                "integrated": None,
            },
        }
    }


def _order_payload(order_id: str, status: str, filled_quantity: str) -> JsonObject:
    return {
        "result": {
            "orderId": order_id,
            "symbol": "005930",
            "side": "BUY",
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "status": status,
            "price": "75000",
            "quantity": "1",
            "orderAmount": None,
            "currency": "KRW",
            "orderedAt": "2026-03-29T09:30:00+09:00",
            "canceledAt": None,
            "execution": {
                "filledQuantity": filled_quantity,
                "averageFilledPrice": "75000" if filled_quantity != "0" else None,
                "filledAmount": "75000" if filled_quantity != "0" else None,
                "commission": "0",
                "tax": None,
                "filledAt": "2026-03-29T09:31:00+09:00" if filled_quantity != "0" else None,
                "settlementDate": "2026-04-01" if filled_quantity != "0" else None,
            },
        }
    }
