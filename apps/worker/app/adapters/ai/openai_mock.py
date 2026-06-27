from __future__ import annotations

from app.domain.news_intel.entities import NewsClassification


class OpenAIMock:
    async def provider_health(self) -> bool:
        return True

    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        return NewsClassification(
            symbol=symbol,
            relevance_score=0.5,
            sentiment="neutral",
            event_type="other",
            risk_level="low",
            summary_short="모의 분류 결과입니다.",
            trading_relevance=0.5,
            confidence=0.6,
        )

