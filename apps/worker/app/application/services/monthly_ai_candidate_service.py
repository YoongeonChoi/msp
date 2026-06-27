from __future__ import annotations

from dataclasses import dataclass, replace

from app.application.ports.ai_port import AIPort
from app.application.ports.repository_port import RepositoryPort
from app.domain.strategy.research import AIUpgradeCandidate, MonthlyResearchDataset


@dataclass(frozen=True, slots=True)
class MonthlyAICandidateService:
    repository: RepositoryPort
    ai: AIPort

    async def propose(self, dataset: MonthlyResearchDataset) -> AIUpgradeCandidate:
        candidate = await self.ai.generate_monthly_candidate(dataset.payload)
        proposed_candidate = replace(
            candidate,
            base_strategy_version_id=dataset.base_strategy_version_id,
            base_strategy_version=dataset.base_strategy_version,
            status="proposed",
            approval_required=True,
        )
        await self.repository.persist_ai_upgrade_candidate(proposed_candidate)
        await self.repository.record_engine_event(
            "info",
            "strategy_research",
            "monthly_ai_candidate_proposed",
            {
                "month": dataset.period.value,
                "candidate_name": proposed_candidate.candidate_name,
                "base_strategy_version": proposed_candidate.base_strategy_version,
                "status": proposed_candidate.status,
                "approval_required": proposed_candidate.approval_required,
            },
        )
        return proposed_candidate
