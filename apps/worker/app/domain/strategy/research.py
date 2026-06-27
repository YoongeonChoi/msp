from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class MonthPeriod:
    value: str
    start_date: date
    end_date: date

    @classmethod
    def from_string(cls, value: str) -> MonthPeriod:
        year_text, month_text = value.split("-", maxsplit=1)
        year = int(year_text)
        month = int(month_text)
        start_date = date(year, month, 1)
        end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return cls(value=value, start_date=start_date, end_date=end_date)

    @property
    def start_datetime(self) -> datetime:
        return datetime.combine(self.start_date, datetime.min.time(), tzinfo=UTC)

    @property
    def end_datetime(self) -> datetime:
        return datetime.combine(self.end_date, datetime.min.time(), tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CandidateWeights:
    technical: float
    fundamental: float
    market_sector: float
    news_event: float
    portfolio: float

    def to_json(self) -> JsonObject:
        return {
            "technical": self.technical,
            "fundamental": self.fundamental,
            "market_sector": self.market_sector,
            "news_event": self.news_event,
            "portfolio": self.portfolio,
        }


@dataclass(frozen=True, slots=True)
class AIUpgradeCandidate:
    id: UUID
    base_strategy_version_id: UUID | None
    base_strategy_version: str
    candidate_name: str
    candidate_weights: CandidateWeights
    candidate_params: JsonObject
    rationale: str
    expected_improvement: str
    risk_notes: str
    required_backtests: list[str]
    approval_required: bool
    status: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def proposed(
        cls,
        base_strategy_version_id: UUID | None,
        base_strategy_version: str,
        candidate_name: str,
        candidate_weights: CandidateWeights,
        candidate_params: JsonObject,
        rationale: str,
        expected_improvement: str,
        risk_notes: str,
        required_backtests: list[str],
        approval_required: bool,
    ) -> AIUpgradeCandidate:
        return cls(
            id=uuid4(),
            base_strategy_version_id=base_strategy_version_id,
            base_strategy_version=base_strategy_version,
            candidate_name=candidate_name,
            candidate_weights=candidate_weights,
            candidate_params=candidate_params,
            rationale=rationale,
            expected_improvement=expected_improvement,
            risk_notes=risk_notes,
            required_backtests=required_backtests,
            approval_required=approval_required,
            status="proposed",
        )


@dataclass(frozen=True, slots=True)
class MonthlyResearchRows:
    base_strategy_version_id: UUID | None
    base_strategy_version: str
    decisions: list[JsonObject]
    outcomes: list[JsonObject]
    orders: list[JsonObject]
    news_events: list[JsonObject]
    features_daily: list[JsonObject]
    api_health: list[JsonObject]
    backtest_runs: list[JsonObject]


@dataclass(frozen=True, slots=True)
class MonthlyResearchDataset:
    period: MonthPeriod
    base_strategy_version_id: UUID | None
    base_strategy_version: str
    payload: JsonObject
