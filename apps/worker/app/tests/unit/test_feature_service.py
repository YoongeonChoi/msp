from __future__ import annotations

from app.application.services.feature_service import FeatureService
from app.domain.common.time import now_utc
from app.domain.fundamentals.entities import QuarterlyFundamentals
from app.domain.market_data.entities import MarketSectorEvidence
from app.domain.news_intel.entities import NewsClassification, NewsEvent
from app.domain.trading.entities import Quote


async def test_live_features_require_market_sector_evidence() -> None:
    service = FeatureService(
        fundamentals=FundamentalsWithValuation(),
        news=PositiveNews(),
        fundamentals_provider_name="opendart",
        news_provider_name="naver",
    )

    features = await service.build_live_features("005930", _quote())

    assert features.raw["live_trading_ready"] is False
    assert features.market_sector_score == 0.50
    assert features.raw["market_sector_source"] == "missing_live_sector_provider"
    assert features.raw["feature_unready_reasons"] == [
        "market_sector_evidence_missing"
    ]


async def test_live_features_require_positive_valuation_inputs() -> None:
    service = FeatureService(
        fundamentals=FundamentalsWithoutValuation(),
        news=PositiveNews(),
        fundamentals_provider_name="opendart",
        news_provider_name="naver",
    )

    features = await service.build_live_features("005930", _quote())

    assert features.raw["live_trading_ready"] is False
    assert features.raw["fundamentals_missing_live_fields"] == ["per", "pbr"]
    assert set(features.raw["feature_unready_reasons"]) == {
        "market_sector_evidence_missing",
        "fundamentals_valuation_missing",
    }


async def test_live_features_accept_provider_market_sector_evidence() -> None:
    service = FeatureService(
        fundamentals=FundamentalsWithValuation(),
        news=PositiveNews(),
        market_sector=VerifiedMarketSector(),
        fundamentals_provider_name="opendart",
        news_provider_name="naver",
        market_sector_provider_name="krx_sector",
    )

    features = await service.build_live_features("005930", _quote())

    assert features.raw["live_trading_ready"] is True
    assert features.raw["feature_unready_reasons"] == []
    assert features.market_sector_score == 0.60
    assert features.raw["market_sector_provider"] == "krx_sector"
    assert features.raw["market_sector_source"] == "krx_sector"
    assert features.raw["market_sector"] == {
        "symbol": "005930",
        "market": "KOSPI",
        "sector": "Information Technology",
        "industry": "Semiconductors",
        "as_of": MARKET_SECTOR_AS_OF.isoformat(),
    }


async def test_live_features_reject_mock_market_sector_evidence() -> None:
    service = FeatureService(
        fundamentals=FundamentalsWithValuation(),
        news=PositiveNews(),
        market_sector=MockMarketSector(),
        fundamentals_provider_name="opendart",
        news_provider_name="naver",
        market_sector_provider_name="fixture",
    )

    features = await service.build_live_features("005930", _quote())

    assert features.raw["live_trading_ready"] is False
    assert "market_sector_source_not_live_provider" in features.raw[
        "feature_unready_reasons"
    ]


class FundamentalsWithValuation:
    async def provider_health(self) -> bool:
        return True

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals:
        return QuarterlyFundamentals(
            symbol=symbol,
            per=11.0,
            pbr=1.0,
            roe=0.18,
            operating_margin=0.22,
            debt_ratio=0.30,
        )


class FundamentalsWithoutValuation:
    async def provider_health(self) -> bool:
        return True

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals:
        return QuarterlyFundamentals(
            symbol=symbol,
            per=None,
            pbr=None,
            roe=0.18,
            operating_margin=0.22,
            debt_ratio=0.30,
        )


class PositiveNews:
    async def provider_health(self) -> bool:
        return True

    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        return [
            NewsEvent(
                symbol=symbol,
                title="Provider backed positive catalyst",
                source="naver",
                published_at=now_utc(),
                classification=NewsClassification(
                    symbol=symbol,
                    relevance_score=0.9,
                    sentiment="positive",
                    event_type="earnings",
                    risk_level="low",
                    summary_short="positive provider-backed fixture",
                    trading_relevance=0.9,
                    confidence=0.9,
                ),
            )
        ]


MARKET_SECTOR_AS_OF = now_utc()


class VerifiedMarketSector:
    async def provider_health(self) -> bool:
        return True

    async def get_sector(self, symbol: str) -> MarketSectorEvidence:
        return MarketSectorEvidence(
            symbol=symbol,
            market="KOSPI",
            sector="Information Technology",
            industry="Semiconductors",
            source="krx_sector",
            as_of=MARKET_SECTOR_AS_OF,
        )


class MockMarketSector:
    async def provider_health(self) -> bool:
        return True

    async def get_sector(self, symbol: str) -> MarketSectorEvidence:
        return MarketSectorEvidence(
            symbol=symbol,
            market="KOSPI",
            sector="Information Technology",
            industry="Semiconductors",
            source="fixture",
            as_of=MARKET_SECTOR_AS_OF,
        )


def _quote() -> Quote:
    return Quote(
        symbol="005930",
        price_krw=75_000,
        as_of=now_utc(),
        source="toss",
    )
