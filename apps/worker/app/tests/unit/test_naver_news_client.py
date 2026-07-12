from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.news.naver_news_client import NaverNewsClient
from app.domain.common.errors import ProviderAuthError, ProviderRateLimitError, ProviderSchemaError


async def test_naver_news_client_uses_official_search_news_request_and_parses_items() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/search/news.json"
        assert request.headers["x-naver-client-id"] == "client-id"
        assert request.headers["x-naver-client-secret"] == "client-secret"
        query = parse_qs(request.url.query.decode())
        assert query == {
            "query": ["005930"],
            "display": ["10"],
            "start": ["1"],
            "sort": ["date"],
        }
        return httpx.Response(
            200,
            json={
                "lastBuildDate": "Sun, 28 Jun 2026 10:00:00 +0900",
                "total": 1,
                "start": 1,
                "display": 1,
                "items": [
                    {
                        "title": "<b>삼성전자</b> 실적 발표",
                        "originallink": "https://example.com/news/1",
                        "link": "https://n.news.naver.com/article/1",
                        "description": "보수적으로 &quot;미분류&quot; 처리합니다.",
                        "pubDate": "Sun, 28 Jun 2026 09:30:00 +0900",
                    }
                ],
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = NaverNewsClient(
        client_id="client-id",
        client_secret="client-secret",
        client=http_client,
    )

    events = await client.get_recent("005930")

    assert len(requests) == 1
    assert events[0].title == "삼성전자 실적 발표"
    assert events[0].published_at.isoformat() == "2026-06-28T00:30:00+00:00"
    assert events[0].classification.sentiment == "unknown"
    assert events[0].classification.risk_level == "unknown"
    assert events[0].classification.summary_short == '보수적으로 "미분류" 처리합니다.'
    await http_client.aclose()


async def test_naver_news_client_fails_closed_without_credentials() -> None:
    client = NaverNewsClient()

    assert await client.provider_health() is False
    with pytest.raises(ProviderAuthError) as exc_info:
        await client.get_recent("005930")

    assert exc_info.value.safe_message == "naver_credentials_missing"
    await client.aclose()


async def test_naver_news_client_maps_official_error_envelope_to_safe_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=json.dumps(
                {
                    "errorMessage": "Too many requests",
                    "errorCode": "SE99",
                }
            ),
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = NaverNewsClient(
        client_id="client-id",
        client_secret="client-secret",
        client=http_client,
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await client.get_recent("005930")

    assert exc_info.value.safe_message == "naver_SE99"
    await http_client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"pubDate": "not-a-provider-date"},
        {"title": "x" * 501},
    ],
)
async def test_naver_news_client_rejects_invalid_item_semantics(
    mutation: dict[str, str],
) -> None:
    item = {
        "title": "정상 제목",
        "originallink": "https://news.example/article",
        "link": "https://news.example/article",
        "description": "정상 설명",
        "pubDate": "Sun, 28 Jun 2026 09:30:00 +0900",
    }
    item.update(mutation)
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "lastBuildDate": "Sun, 28 Jun 2026 10:00:00 +0900",
                    "total": 1,
                    "start": 1,
                    "display": 1,
                    "items": [item],
                },
                request=request,
            )
        )
    )
    client = NaverNewsClient("client-id", "client-secret", http_client)

    with pytest.raises(ProviderSchemaError, match="naver_news_schema_mismatch"):
        await client.get_recent("005930")
    await http_client.aclose()


async def test_naver_news_client_rejects_more_than_requested_items() -> None:
    item = {
        "title": "정상 제목",
        "originallink": "",
        "link": "",
        "description": "정상 설명",
        "pubDate": "Sun, 28 Jun 2026 09:30:00 +0900",
    }
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "lastBuildDate": "Sun, 28 Jun 2026 10:00:00 +0900",
                    "total": 11,
                    "start": 1,
                    "display": 10,
                    "items": [item] * 11,
                },
                request=request,
            )
        )
    )
    client = NaverNewsClient("client-id", "client-secret", http_client)

    with pytest.raises(ProviderSchemaError, match="naver_news_schema_mismatch"):
        await client.get_recent("005930")
    await http_client.aclose()


async def test_naver_news_client_bounds_decompressed_response_bytes() -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 256_001, request=request)
        )
    )
    client = NaverNewsClient("client-id", "client-secret", http_client)

    with pytest.raises(ProviderSchemaError, match="naver_news_response_too_large"):
        await client.get_recent("005930")
    await http_client.aclose()
