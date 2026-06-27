from __future__ import annotations

from app.application.ports.ai_port import AIPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.monthly_ai_candidate_service import MonthlyAICandidateService
from app.application.services.monthly_dataset_builder import MonthlyResearchDatasetBuilder
from app.domain.strategy.research import AIUpgradeCandidate, MonthPeriod


class MonthlyResearchService:
    def __init__(self, repository: RepositoryPort, ai: AIPort) -> None:
        self.repository = repository
        self.ai = ai
        self.dataset_builder = MonthlyResearchDatasetBuilder()
        self.candidate_service = MonthlyAICandidateService(repository=repository, ai=ai)

    async def generate_candidate(self, period: MonthPeriod) -> AIUpgradeCandidate:
        rows = await self.repository.load_monthly_research_rows(period)
        dataset = self.dataset_builder.build(period, rows)
        return await self.candidate_service.propose(dataset)
