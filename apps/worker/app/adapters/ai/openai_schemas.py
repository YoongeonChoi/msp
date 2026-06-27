from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class MonthlyUpgradeCandidateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_strategy_version: str
    candidate_name: str
    candidate_weights: dict[str, float]
    candidate_params: dict[str, object]
    rationale: str
    expected_improvement: str
    risk_notes: str
    required_backtests: list[str]
    approval_required: bool = True

