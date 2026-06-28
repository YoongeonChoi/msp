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


class MonthlyCandidateParamsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buy_threshold: float = Field(ge=0, le=1)
    sell_threshold: float = Field(ge=0, le=1)
    max_position_pct: float = Field(ge=0, le=1)
    news_risk_penalty: float = Field(ge=0, le=1)


class MonthlyUpgradeCandidateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_strategy_version: str
    candidate_name: str
    candidate_weights: MonthlyCandidateWeightsSchema
    candidate_params: MonthlyCandidateParamsSchema
    rationale: str
    expected_improvement: str
    risk_notes: str
    required_backtests: list[str]
    approval_required: Literal[True]

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
            candidate_params=json_object(self.candidate_params.model_dump()),
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
    number_field = {"type": "number"}
    return json_object(
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "base_strategy_version",
                "candidate_name",
                "candidate_weights",
                "candidate_params",
                "rationale",
                "expected_improvement",
                "risk_notes",
                "required_backtests",
                "approval_required",
            ],
            "properties": {
                "base_strategy_version": {"type": "string"},
                "candidate_name": {"type": "string"},
                "candidate_weights": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "technical",
                        "fundamental",
                        "market_sector",
                        "news_event",
                        "portfolio",
                    ],
                    "properties": {
                        "technical": number_field,
                        "fundamental": number_field,
                        "market_sector": number_field,
                        "news_event": number_field,
                        "portfolio": number_field,
                    },
                },
                "candidate_params": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "buy_threshold",
                        "sell_threshold",
                        "max_position_pct",
                        "news_risk_penalty",
                    ],
                    "properties": {
                        "buy_threshold": number_field,
                        "sell_threshold": number_field,
                        "max_position_pct": number_field,
                        "news_risk_penalty": number_field,
                    },
                },
                "rationale": {"type": "string"},
                "expected_improvement": {"type": "string"},
                "risk_notes": {"type": "string"},
                "required_backtests": {"type": "array", "items": {"type": "string"}},
                "approval_required": {"type": "boolean", "enum": [True]},
            },
        }
    )
