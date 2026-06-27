from __future__ import annotations

from app.domain.common.errors import ProviderUnavailableError
from app.domain.news_intel.entities import NewsClassification


class OpenAIClient:
    async def provider_health(self) -> bool:
        return False

    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        raise ProviderUnavailableError("openai", "openai_structured_output_not_configured")
