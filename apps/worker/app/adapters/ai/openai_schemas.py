from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.common.errors import ProviderSchemaError
from app.domain.common.json import JsonObject, json_object
from app.domain.strategy.research import AIUpgradeCandidate, CandidateWeights


class NewsClassificationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    relevance_score: float = Field(ge=0, le=1)
    sentiment: Literal["positive", "neutral", "negative", "unknown"]
    event_type: Literal[
        "earnings",
        "contract",
        "lawsuit",
        "regulation",
        "management",
        "supply_chain",
        "capital_raise",
        "dividend",
        "macro",
        "analyst",
        "rumor",
        "other",
    ]
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    summary_short: str = Field(max_length=240)
    trading_relevance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


class MonthlyCandidateWeightsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technical: float = Field(ge=0, le=1)
    fundamental: float = Field(ge=0, le=1)
    market_sector: float = Field(ge=0, le=1)
    news_event: float = Field(ge=0, le=1)
    portfolio: float = Field(ge=0, le=1)


class MonthlyUpgradeCandidateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_strategy_version: str
    candidate_name: str
    candidate_weights: MonthlyCandidateWeightsSchema
    candidate_params: dict[str, str | int | float | bool | None]
    rationale: str
    expected_improvement: str
    risk_notes: str
    required_backtests: list[str]
    approval_required: Literal[True] = True

    def to_domain(self, base_strategy_version_id: UUID | None) -> AIUpgradeCandidate:
        return AIUpgradeCandidate.proposed(
            base_strategy_version_id=base_strategy_version_id,
            base_strategy_version=self.base_strategy_version,
            candidate_name=self.candidate_name,
            candidate_weights=CandidateWeights(
                technical=self.candidate_weights.technical,
                fundamental=self.candidate_weights.fundamental,
                market_sector=self.candidate_weights.market_sector,
                news_event=self.candidate_weights.news_event,
                portfolio=self.candidate_weights.portfolio,
            ),
            candidate_params=json_object(self.candidate_params),
            rationale=self.rationale,
            expected_improvement=self.expected_improvement,
            risk_notes=self.risk_notes,
            required_backtests=list(self.required_backtests),
            approval_required=self.approval_required,
        )
def parse_monthly_candidate_json(raw_text: str) -> MonthlyUpgradeCandidateSchema:
    try:
        decoded = json.loads(raw_text)
        return MonthlyUpgradeCandidateSchema.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ProviderSchemaError("openai", "invalid_monthly_candidate_schema") from exc


def monthly_candidate_response_format_schema() -> JsonObject:
    return json_object(MonthlyUpgradeCandidateSchema.model_json_schema())
