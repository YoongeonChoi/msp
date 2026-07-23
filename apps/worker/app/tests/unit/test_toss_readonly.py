from __future__ import annotations

import gzip
import json
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, cast
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

_TOKEN_RESPONSE_LIMIT_BYTES = 64 * 1024
_READ_RESPONSE_LIMIT_BYTES = 4 * 1024 * 1024


class AlwaysEqualText(str):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class TossCandleQuerySubclass(TossCandleQuery):
    pass


async def test_toss_auth_uses_client_credentials_form_token_flow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        assert request.method == "POST"
        assert request.url.path == "/oauth2/token"
        assert request.headers["accept-encoding"] == "identity"
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


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(
                b'{"access_token":"token-value","token_type":"Bearer","expires_in":3600}'
            ),
        ),
        httpx.Response(
            200,
            content=(
                b'{"access_token":"token-value","token_type":"Bearer",'
                b'"expires_in":3600}' + b" " * _TOKEN_RESPONSE_LIMIT_BYTES
            ),
        ),
        httpx.Response(
            200,
            content=(
                b'{"access_token":"token-a","access_token":"token-b",'
                b'"token_type":"Bearer","expires_in":3600}'
            ),
        ),
        httpx.Response(
            200,
            content=(
                b'{"access_token":"token-value","token_type":"Bearer",'
                b'"expires_in":3600,"metadata":{"key":1,"key":1}}'
            ),
        ),
    ],
)
async def test_toss_auth_rejects_encoded_oversized_or_duplicate_json(
    response: httpx.Response,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=client)
    try:
        with pytest.raises(ProviderSchemaError, match="toss_auth_schema_invalid"):
            await auth.access_token()
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert requests[0].headers["accept-encoding"] == "identity"


async def test_toss_auth_rejects_extra_token_field() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "token-value",
                "token_type": "Bearer",
                "expires_in": 3600,
                "unexpected": "secret-token-metadata",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=client)
    try:
        with pytest.raises(ProviderSchemaError, match="toss_auth_schema_invalid"):
            await auth.access_token()
    finally:
        await client.aclose()


async def test_toss_auth_accepts_response_at_exact_size_limit() -> None:
    canonical = b'{"access_token":"token-value","token_type":"Bearer","expires_in":3600}'

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(canonical + b" " * (_TOKEN_RESPONSE_LIMIT_BYTES - len(canonical))),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=client)
    try:
        assert await auth.access_token() == "token-value"
    finally:
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
        if request.url.path == "/api/v1/holdings" and "x-tossinvest-account" in request.headers
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
            "/api/v1/buying-power": {"result": {"currency": "KRW", "cashBuyingPower": "5000000"}},
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
    assert calendar.next_business_day.integrated is not None
    assert calendar.next_business_day.integrated.regular_market is not None
    assert calendar.next_business_day.integrated.regular_market.start_time.hour == 9
    calendar_request = next(
        request for request in requests if request.url.path == "/api/v1/market-calendar/KR"
    )
    assert calendar_request.url.params["date"] == "2026-03-25"
    assert account_state.cash_krw == 5_000_000
    assert account_state.equity_krw == 12_050_000
    assert account_state.daily_loss_pct == 0.0
    assert account_state.daily_order_count == 0
    assert account_state.daily_order_count_verified is False
    account_paths = [
        request.url.path for request in requests if "x-tossinvest-account" in request.headers
    ]
    assert account_paths == [
        "/api/v1/buying-power",
        "/api/v1/buying-power",
        "/api/v1/holdings",
    ]


@pytest.mark.parametrize("missing", ["today", "next_regular_end"])
async def test_toss_client_rejects_missing_required_calendar_fields(
    missing: str,
) -> None:
    payload = _kr_market_calendar_payload()
    result = cast(dict[str, Any], payload["result"])
    if missing == "today":
        del result["today"]
    else:
        next_day = cast(dict[str, Any], result["nextBusinessDay"])
        integrated = cast(dict[str, Any], next_day["integrated"])
        regular = cast(dict[str, Any], integrated["regularMarket"])
        del regular["endTime"]
    client, _requests = _client_with_responses({"/api/v1/market-calendar/KR": payload})

    with pytest.raises(ProviderSchemaError, match="toss_read_schema_invalid"):
        await client.get_kr_market_calendar(date(2026, 3, 25))


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
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)


@pytest.mark.parametrize(
    "response_case",
    [
        "encoded",
        "oversized",
        "duplicate",
        "nested_duplicate",
    ],
)
async def test_toss_candle_read_rejects_encoded_oversized_or_duplicate_json(
    response_case: str,
) -> None:
    canonical_body = json.dumps(
        _candle_response_payload(),
        separators=(",", ":"),
    ).encode()
    responses = {
        "encoded": httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(canonical_body),
        ),
        "oversized": httpx.Response(
            200,
            content=canonical_body + b" " * _READ_RESPONSE_LIMIT_BYTES,
        ),
        "duplicate": httpx.Response(
            200,
            content=_duplicate_candle_envelope_bytes(),
        ),
        "nested_duplicate": httpx.Response(
            200,
            content=_nested_duplicate_candle_bytes(),
        ),
    }
    response = responses[response_case]
    client, requests = _client_with_raw_response(response)
    try:
        with pytest.raises(ProviderSchemaError, match="toss_read_schema_invalid"):
            await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=1))
    finally:
        await client.client.aclose()

    assert [request.url.path for request in requests] == [
        "/oauth2/token",
        "/api/v1/candles",
    ]
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)


async def test_toss_candle_read_accepts_response_at_exact_size_limit() -> None:
    canonical = json.dumps(
        _candle_response_payload(),
        separators=(",", ":"),
    ).encode()
    response = httpx.Response(
        200,
        content=(canonical + b" " * (_READ_RESPONSE_LIMIT_BYTES - len(canonical))),
    )
    client, _requests = _client_with_raw_response(response)
    try:
        page = await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=1))
    finally:
        await client.client.aclose()

    assert len(page.candles) == 1


@pytest.mark.parametrize(
    "query",
    [
        TossCandleQuerySubclass(symbol="005930", interval="1d", count=1),
        TossCandleQuery(
            symbol="005930",
            interval=cast(Any, AlwaysEqualText("1d")),
            count=1,
        ),
    ],
)
async def test_toss_client_rejects_candle_query_subtypes_before_network(
    query: TossCandleQuery,
) -> None:
    client, requests = _client_with_responses({})

    with pytest.raises(ProviderSchemaError, match="toss_candle_query_invalid"):
        await client.get_candles(query)

    assert requests == []


@pytest.mark.parametrize("extra_scope", ["envelope", "page", "candle"])
async def test_toss_candle_read_rejects_extra_fields_at_every_scope(
    extra_scope: str,
) -> None:
    payload = _candle_response_payload()
    if extra_scope == "envelope":
        payload["unexpected"] = "secret-envelope-field"
    elif extra_scope == "page":
        page = cast(dict[str, Any], payload["result"])
        page["hasNext"] = False
    else:
        page = cast(dict[str, Any], payload["result"])
        candle = cast(list[dict[str, Any]], page["candles"])[0]
        candle["unexpected"] = "secret-candle-field"

    client, _requests = _client_with_raw_response(httpx.Response(200, json=payload))
    try:
        with pytest.raises(ProviderSchemaError, match="toss_read_schema_invalid"):
            await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=1))
    finally:
        await client.client.aclose()


async def test_toss_candle_transport_error_does_not_expose_response_body() -> None:
    secret = "secret-candle-response-must-not-leak"
    client, _requests = _client_with_raw_response(httpx.Response(200, content=secret.encode()))
    try:
        with pytest.raises(ProviderSchemaError) as captured:
            await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=1))
    finally:
        await client.client.aclose()

    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in str(captured.value)
    assert secret not in formatted


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

    assert exc_info.value.safe_message == "toss_http_429"


async def test_toss_provider_error_body_cannot_inject_safe_message() -> None:
    secret = "SECRET_SENSITIVE_123\nforged-log-line"
    client, _requests = _client_with_responses(
        {
            "/api/v1/accounts": (
                429,
                {
                    "error": {
                        "requestId": "request-id",
                        "code": secret,
                        "message": secret,
                    }
                },
            )
        }
    )

    with pytest.raises(ProviderRateLimitError) as captured:
        await client.list_accounts()

    error = captured.value
    formatted = "".join(traceback.format_exception(error))
    assert error.safe_message == "toss_http_429"
    assert secret not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_toss_auth_error_body_cannot_inject_safe_message() -> None:
    secret = "SECRET_AUTH_123\nforged-log-line"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": secret,
                "error_description": secret,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(_settings(), client=client)
    try:
        with pytest.raises(ProviderAuthError) as captured:
            await auth.access_token()
    finally:
        await client.aclose()

    error = captured.value
    formatted = "".join(traceback.format_exception(error))
    assert error.safe_message == "toss_http_401"
    assert secret not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


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


def _client_with_raw_response(
    response: httpx.Response,
) -> tuple[TossClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "token-value",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return response

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = TossAuth(settings, client=http_client)
    return TossClient(settings, auth=auth, client=http_client), requests


def _candle_response_payload() -> JsonObject:
    return {
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
    }


def _duplicate_candle_envelope_bytes() -> bytes:
    page = json.dumps(
        _candle_response_payload()["result"],
        separators=(",", ":"),
    ).encode()
    return b'{"result":' + page + b',"result":' + page + b"}"


def _nested_duplicate_candle_bytes() -> bytes:
    return (
        b'{"result":{"candles":[{"timestamp":'
        b'"2026-03-25T09:00:00+09:00","openPrice":"71600",'
        b'"highPrice":"72300","lowPrice":"71500","closePrice":"72000",'
        b'"volume":"3521000","currency":"KRW","currency":"KRW"}],'
        b'"nextBefore":null}}'
    )


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
                "integrated": {
                    "regularMarket": {
                        "startTime": "2026-03-26T09:00:00+09:00",
                        "singlePriceAuctionStartTime": ("2026-03-26T15:20:00+09:00"),
                        "endTime": "2026-03-26T15:30:00+09:00",
                    }
                },
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
