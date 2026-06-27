from uuid import uuid4

from app.application.services.signal_service import WeightedFactorStrategyV1
from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.trading.value_objects import StrategyWeights


def test_weighted_factor_score_stays_in_range() -> None:
    signal = WeightedFactorStrategyV1().score(
        FeatureVector(
            symbol="005930",
            technical_score=2.0,
            fundamental_score=1.0,
            market_sector_score=1.0,
            news_event_score=1.0,
            portfolio_score=1.0,
        ),
        StrategyContext(strategy_version_id=uuid4(), weights=StrategyWeights()),
    )

    assert 0 <= signal.final_score <= 1

