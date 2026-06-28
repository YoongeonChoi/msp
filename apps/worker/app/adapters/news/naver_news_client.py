from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.common.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.news_intel.entities import NewsClassification, NewsEvent

NAVER_NEWS_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"


class NaverNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    originallink: str = ""
    link: str = ""
    description: str
    pubDate: str


class NaverNewsSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lastBuildDate: str
    total: int
    start: int
    display: int
    items: list[NaverNewsItem]


class NaverErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    errorMessage: str = ""
    errorCode: str = Field(default="unknown")


class NaverNewsClient:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.headers = {
            "X-Naver-Client-Id": client_id or "",
            "X-Naver-Client-Secret": client_secret or "",
        }
        self.client = client or httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def provider_health(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        if not self.client_id or not self.client_secret:
            raise ProviderAuthError("naver", "naver_credentials_missing")
        try:
            response = await self.client.get(
                NAVER_NEWS_SEARCH_URL,
                headers=self.headers,
                params={"query": symbol, "display": 10, "start": 1, "sort": "date"},
            )
            if response.is_error:
                raise _provider_error_from_response(response)
            payload = NaverNewsSearchResponse.model_validate_json(response.text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("naver", "naver_timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("naver", "naver_http_error") from exc
        except ValidationError as exc:
            raise ProviderSchemaError("naver", "naver_news_schema_mismatch") from exc
        return [_to_news_event(symbol, item) for item in payload.items]

    async def aclose(self) -> None:
        await self.client.aclose()


def _to_news_event(symbol: str, item: NaverNewsItem) -> NewsEvent:
    summary = _clean_html(item.description)[:240]
    title = _clean_html(item.title)
    return NewsEvent(
        symbol=symbol,
        title=title,
        source="naver",
        published_at=_parse_pub_date(item.pubDate),
        classification=NewsClassification(
            symbol=symbol,
            relevance_score=0.0,
            sentiment="unknown",
            event_type="news",
            risk_level="unknown",
            summary_short=summary,
            trading_relevance=0.0,
            confidence=0.0,
        ),
    )


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(value))).strip()


def _parse_pub_date(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _provider_error_from_response(response: httpx.Response) -> ProviderError:
    safe_code = _safe_error_code(response)
    match response.status_code:
        case 400:
            return ProviderSchemaError("naver", safe_code)
        case 401 | 403:
            return ProviderAuthError("naver", safe_code)
        case 429:
            return ProviderRateLimitError("naver", safe_code)
        case 500 | 502 | 503 | 504:
            return ProviderUnavailableError("naver", safe_code)
        case _:
            return ProviderUnknownError("naver", safe_code)


def _safe_error_code(response: httpx.Response) -> str:
    try:
        envelope = NaverErrorEnvelope.model_validate_json(response.text)
    except ValidationError:
        return f"naver_http_{response.status_code}"
    return f"naver_{envelope.errorCode}"
