from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.application.ports.ai_port import AIPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.news_port import NewsPort
from app.application.ports.repository_port import RepositoryPort
from app.domain.common.errors import KnownFailClosedError

HealthCheck = Callable[[], Awaitable[bool]]
SENSITIVE_DETAIL_PATTERN = re.compile(
    r"authorization|secret|token|api[_-]?key|apikey|client_secret|password|jwt|bearer\s+|sk-",
    re.IGNORECASE,
)


class HealthService:
    def __init__(
        self,
        repository: RepositoryPort,
        broker: BrokerPort,
        market_data: MarketDataPort,
        fundamentals: FundamentalsPort,
        news: NewsPort,
        ai: AIPort,
        market_data_provider_name: str = "krx",
    ) -> None:
        self.repository = repository
        self.providers: dict[str, HealthCheck] = {
            "toss": broker.provider_health,
            market_data_provider_name: market_data.provider_health,
            "opendart": fundamentals.provider_health,
            "naver": news.provider_health,
            "openai": ai.provider_health,
        }

    async def check(self) -> dict[str, bool]:
        result: dict[str, bool] = {"supabase": True}
        for provider, check in self.providers.items():
            details: dict[str, object] = {}
            try:
                healthy = await check()
            except KnownFailClosedError as exc:
                healthy = False
                details = _safe_details(
                    {
                        "error_type": type(exc).__name__,
                        "reason": exc.safe_message,
                    }
                )
            except Exception as exc:
                healthy = False
                details = {"error_type": type(exc).__name__}
            if not healthy and not details:
                details = _provider_health_details(check)
            result[provider] = healthy
            await self.repository.record_api_health(provider, healthy, details)
        return result


def _provider_health_details(check: HealthCheck) -> dict[str, object]:
    owner = getattr(check, "__self__", None)
    details_method = getattr(owner, "provider_health_details", None)
    if not callable(details_method):
        return {}
    details = details_method()
    if not isinstance(details, Mapping):
        return {}
    return _safe_details(details)


def _safe_details(details: Mapping[str, Any]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in details.items():
        if not isinstance(key, str) or not key.isidentifier():
            continue
        if SENSITIVE_DETAIL_PATTERN.search(key):
            continue
        if isinstance(value, str):
            if SENSITIVE_DETAIL_PATTERN.search(value):
                safe[key] = "[redacted]"
            else:
                safe[key] = value[:160]
        elif isinstance(value, (bool, int, float)) or value is None:
            safe[key] = value
    return safe
