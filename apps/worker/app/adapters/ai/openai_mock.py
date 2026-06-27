from __future__ import annotations

from app.domain.common.json import JsonObject
from app.domain.news_intel.entities import NewsClassification
from app.domain.strategy.research import AIUpgradeCandidate, CandidateWeights


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

    async def generate_monthly_candidate(self, dataset_payload: JsonObject) -> AIUpgradeCandidate:
        base_strategy = str(
            dataset_payload.get("base_strategy_version", "strategy_v1_weighted_factor")
        )
        return AIUpgradeCandidate.proposed(
            base_strategy_version_id=None,
            base_strategy_version=base_strategy,
            candidate_name="mock_monthly_candidate",
            candidate_weights=CandidateWeights(
                technical=0.34,
                fundamental=0.24,
                market_sector=0.15,
                news_event=0.17,
                portfolio=0.10,
            ),
            candidate_params={"buy_threshold": 0.69, "sell_threshold": 0.25},
            rationale="월간 mock dataset 기반 후보입니다.",
            expected_improvement="paper 검증용 후보이며 성과 개선을 보장하지 않습니다.",
            risk_notes="반드시 backtest와 paper 검증 후 승인해야 합니다.",
            required_backtests=[
                "out_of_sample",
                "walk_forward",
                "transaction_costs",
                "sector_exposure",
            ],
            approval_required=True,
        )
