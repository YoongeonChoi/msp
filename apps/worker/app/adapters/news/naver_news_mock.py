from __future__ import annotations

from app.domain.common.time import now_utc
from app.domain.news_intel.entities import NewsClassification, NewsEvent


class NaverNewsMock:
    async def provider_health(self) -> bool:
        return True

    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        classification = NewsClassification(
            symbol=symbol,
            relevance_score=0.7,
            sentiment="neutral",
            event_type="other",
            risk_level="low",
            summary_short="모의 뉴스입니다.",
            trading_relevance=0.5,
            confidence=0.6,
        )
        return [
            NewsEvent(
                symbol=symbol,
                title="모의 뉴스",
                source="mock",
                published_at=now_utc(),
                classification=classification,
            )
        ]

