from __future__ import annotations

from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.market_sector_port import MarketSectorPort
from app.application.ports.news_port import NewsPort
from app.domain.common.errors import ProviderError
from app.domain.fundamentals.entities import QuarterlyFundamentals
from app.domain.market_data.entities import MarketSectorEvidence
from app.domain.news_intel.entities import NewsEvent
from app.domain.strategy.entities import FeatureVector
from app.domain.trading.entities import Quote

MOCK_SOURCES = {
    "",
    "mock",
    "mock_static",
    "krx_mock",
    "opendart_mock",
    "naver_mock",
}
MARKET_SECTOR_MOCK_SOURCES = MOCK_SOURCES | {"fixture"}


class FeatureService:
    def __init__(
        self,
        fundamentals: FundamentalsPort | None = None,
        news: NewsPort | None = None,
        market_sector: MarketSectorPort | None = None,
        *,
        fundamentals_provider_name: str = "unconfigured",
        news_provider_name: str = "unconfigured",
        market_sector_provider_name: str = "unconfigured",
    ) -> None:
        self.fundamentals = fundamentals
        self.news = news
        self.market_sector = market_sector
        self.fundamentals_provider_name = fundamentals_provider_name
        self.news_provider_name = news_provider_name
        self.market_sector_provider_name = market_sector_provider_name

    def build_mock_features(self, symbol: str, quote: Quote) -> FeatureVector:
        liquidity = 0.8 if quote.price_krw > 0 else 0.0
        return FeatureVector(
            symbol=symbol,
            technical_score=0.82,
            fundamental_score=0.60,
            market_sector_score=0.60,
            news_event_score=0.65,
            portfolio_score=liquidity,
            raw={
                "quote_price_krw": quote.price_krw,
                "source": quote.source,
                "feature_source": "mock_static",
                "live_trading_ready": False,
            },
        )

    async def build_live_features(self, symbol: str, quote: Quote) -> FeatureVector:
        raw: dict[str, object] = {
            "quote_price_krw": quote.price_krw,
            "quote_as_of": quote.as_of.isoformat(),
            "quote_source": quote.source,
            "source": quote.source,
            "feature_source": "provider_live_v1",
            "feature_evidence_version": "provider_live_v1",
            "live_trading_ready": False,
        }
        unready_reasons: list[str] = []
        if quote.price_krw <= 0:
            unready_reasons.append("quote_price_invalid")
        if _is_mock_source(quote.source):
            unready_reasons.append("quote_source_not_live_provider")

        fundamentals = await self._load_fundamentals(symbol, raw, unready_reasons)
        news_events = await self._load_news(symbol, raw, unready_reasons)
        market_sector = await self._load_market_sector(symbol, raw, unready_reasons)

        technical_score = 0.70 if quote.price_krw > 0 else 0.0
        fundamental_score = _fundamental_score(fundamentals)
        market_sector_score = _market_sector_score(market_sector)
        news_event_score = _news_score(news_events)
        portfolio_score = 0.8 if quote.price_krw > 0 else 0.0
        raw["feature_unready_reasons"] = unready_reasons
        raw["live_trading_ready"] = not unready_reasons
        return FeatureVector(
            symbol=symbol,
            technical_score=technical_score,
            fundamental_score=fundamental_score,
            market_sector_score=market_sector_score,
            news_event_score=news_event_score,
            portfolio_score=portfolio_score,
            raw=raw,
        )

    async def _load_fundamentals(
        self,
        symbol: str,
        raw: dict[str, object],
        unready_reasons: list[str],
    ) -> QuarterlyFundamentals | None:
        raw["fundamentals_source"] = self.fundamentals_provider_name
        if self.fundamentals is None:
            unready_reasons.append("fundamentals_provider_not_configured")
            return None
        if _is_mock_source(self.fundamentals_provider_name):
            unready_reasons.append("fundamentals_source_not_live_provider")
        try:
            fundamentals = await self.fundamentals.get_latest(symbol)
        except ProviderError as exc:
            unready_reasons.append("fundamentals_provider_error")
            raw["fundamentals_error_provider"] = exc.provider
            raw["fundamentals_error_reason"] = exc.safe_message
            return None
        if fundamentals is None:
            unready_reasons.append("fundamentals_missing")
            return None
        raw["fundamentals"] = {
            "per": fundamentals.per,
            "pbr": fundamentals.pbr,
            "roe": fundamentals.roe,
            "operating_margin": fundamentals.operating_margin,
            "debt_ratio": fundamentals.debt_ratio,
        }
        missing_valuation_fields = _missing_required_valuation_fields(fundamentals)
        if missing_valuation_fields:
            raw["fundamentals_missing_live_fields"] = missing_valuation_fields
            unready_reasons.append("fundamentals_valuation_missing")
        if not _has_fundamental_signal(fundamentals):
            unready_reasons.append("fundamentals_values_missing")
        return fundamentals

    async def _load_news(
        self,
        symbol: str,
        raw: dict[str, object],
        unready_reasons: list[str],
    ) -> list[NewsEvent]:
        raw["news_provider"] = self.news_provider_name
        if self.news is None:
            unready_reasons.append("news_provider_not_configured")
            raw["news_event_count"] = 0
            return []
        if _is_mock_source(self.news_provider_name):
            unready_reasons.append("news_provider_not_live_provider")
        try:
            events = await self.news.get_recent(symbol)
        except ProviderError as exc:
            unready_reasons.append("news_provider_error")
            raw["news_error_provider"] = exc.provider
            raw["news_error_reason"] = exc.safe_message
            raw["news_event_count"] = 0
            return []
        raw["news_event_count"] = len(events)
        raw["news_sources"] = sorted({event.source for event in events})
        raw["news_events"] = [_news_event_snapshot(event) for event in events]
        if events:
            raw["latest_news_published_at"] = max(
                event.published_at for event in events
            ).isoformat()
        if not events:
            unready_reasons.append("news_missing")
            return []
        if any(_is_mock_source(event.source) for event in events):
            unready_reasons.append("news_source_not_live_provider")
        return events

    async def _load_market_sector(
        self,
        symbol: str,
        raw: dict[str, object],
        unready_reasons: list[str],
    ) -> MarketSectorEvidence | None:
        raw["market_sector_provider"] = self.market_sector_provider_name
        if self.market_sector is None:
            raw["market_sector_source"] = "missing_live_sector_provider"
            unready_reasons.append("market_sector_evidence_missing")
            return None
        if _is_mock_market_sector_source(self.market_sector_provider_name):
            unready_reasons.append("market_sector_source_not_live_provider")
        try:
            evidence = await self.market_sector.get_sector(symbol)
        except ProviderError as exc:
            unready_reasons.append("market_sector_provider_error")
            raw["market_sector_error_provider"] = exc.provider
            raw["market_sector_error_reason"] = exc.safe_message
            return None
        if evidence is None:
            raw["market_sector_source"] = "missing_live_sector_provider"
            unready_reasons.append("market_sector_evidence_missing")
            return None
        raw["market_sector_source"] = evidence.source
        raw["market_sector"] = {
            "symbol": evidence.symbol,
            "market": evidence.market,
            "sector": evidence.sector,
            "industry": evidence.industry,
            "as_of": evidence.as_of.isoformat(),
        }
        if evidence.symbol != symbol:
            unready_reasons.append("market_sector_symbol_mismatch")
        if _is_mock_market_sector_source(evidence.source):
            unready_reasons.append("market_sector_source_not_live_provider")
        if not evidence.market.strip() or not evidence.sector.strip():
            unready_reasons.append("market_sector_values_missing")
        return evidence


def _is_mock_source(source: str | None) -> bool:
    return source is None or source.strip().lower() in MOCK_SOURCES


def _is_mock_market_sector_source(source: str | None) -> bool:
    return source is None or source.strip().lower() in MARKET_SECTOR_MOCK_SOURCES


def _has_fundamental_signal(fundamentals: QuarterlyFundamentals) -> bool:
    return any(
        value is not None
        for value in (
            fundamentals.per,
            fundamentals.pbr,
            fundamentals.roe,
            fundamentals.operating_margin,
            fundamentals.debt_ratio,
        )
    )


def _missing_required_valuation_fields(
    fundamentals: QuarterlyFundamentals,
) -> list[str]:
    missing: list[str] = []
    if fundamentals.per is None or fundamentals.per <= 0:
        missing.append("per")
    if fundamentals.pbr is None or fundamentals.pbr <= 0:
        missing.append("pbr")
    return missing


def _fundamental_score(fundamentals: QuarterlyFundamentals | None) -> float:
    if fundamentals is None:
        return 0.0
    scores: list[float] = []
    if fundamentals.roe is not None:
        scores.append(_bounded((fundamentals.roe + 0.05) / 0.25))
    if fundamentals.operating_margin is not None:
        scores.append(_bounded(fundamentals.operating_margin / 0.25))
    if fundamentals.debt_ratio is not None:
        scores.append(_bounded(1.0 - (fundamentals.debt_ratio / 2.0)))
    if fundamentals.per is not None and fundamentals.per > 0:
        scores.append(_bounded(1.0 - abs(fundamentals.per - 12.0) / 30.0))
    if fundamentals.pbr is not None and fundamentals.pbr > 0:
        scores.append(_bounded(1.0 - abs(fundamentals.pbr - 1.2) / 4.0))
    return _average(scores, default=0.0)


def _news_score(events: list[NewsEvent]) -> float:
    if not events:
        return 0.0
    return _average([_news_event_score(event) for event in events], default=0.0)


def _market_sector_score(evidence: MarketSectorEvidence | None) -> float:
    if evidence is None:
        return 0.50
    if not evidence.market.strip() or not evidence.sector.strip():
        return 0.0
    return 0.60


def _news_event_score(event: NewsEvent) -> float:
    classification = event.classification
    sentiment_score = {
        "positive": 0.75,
        "neutral": 0.55,
        "negative": 0.25,
        "unknown": 0.45,
    }[classification.sentiment]
    risk_penalty = {
        "low": 0.0,
        "medium": 0.10,
        "high": 0.25,
        "critical": 0.45,
        "unknown": 0.05,
    }[classification.risk_level]
    return _bounded(
        (0.25 * classification.relevance_score)
        + (0.25 * classification.trading_relevance)
        + (0.20 * classification.confidence)
        + (0.30 * sentiment_score)
        - risk_penalty
    )


def _news_event_snapshot(event: NewsEvent) -> dict[str, object]:
    classification = event.classification
    return {
        "symbol": event.symbol,
        "title": event.title,
        "source": event.source,
        "published_at": event.published_at.isoformat(),
        "relevance_score": classification.relevance_score,
        "sentiment": classification.sentiment,
        "event_type": classification.event_type,
        "risk_level": classification.risk_level,
        "summary_short": classification.summary_short,
        "trading_relevance": classification.trading_relevance,
        "confidence": classification.confidence,
    }


def _average(values: list[float], *, default: float) -> float:
    if not values:
        return default
    return _bounded(sum(values) / len(values))


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))
