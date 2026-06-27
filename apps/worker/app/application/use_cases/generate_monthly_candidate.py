from __future__ import annotations

from app.application.services.monthly_research_service import MonthlyResearchService
from app.domain.strategy.research import AIUpgradeCandidate, MonthPeriod


class GenerateMonthlyCandidate:
    def __init__(self, monthly_research_service: MonthlyResearchService) -> None:
        self.monthly_research_service = monthly_research_service

    async def execute(self, month: str) -> AIUpgradeCandidate:
        period = MonthPeriod.from_string(month)
        return await self.monthly_research_service.generate_candidate(period)
