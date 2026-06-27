from __future__ import annotations

from app.domain.strategy.entities import FeatureVector
from app.domain.trading.entities import Quote


class FeatureService:
    def build_mock_features(self, symbol: str, quote: Quote) -> FeatureVector:
        liquidity = 0.8 if quote.price_krw > 0 else 0.0
        return FeatureVector(
            symbol=symbol,
            technical_score=0.82,
            fundamental_score=0.60,
            market_sector_score=0.60,
            news_event_score=0.65,
            portfolio_score=liquidity,
            raw={"quote_price_krw": quote.price_krw, "source": quote.source},
        )
