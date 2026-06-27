from __future__ import annotations

from app.application.ports.repository_port import RepositoryPort


class RetentionService:
    def __init__(self, repository: RepositoryPort) -> None:
        self.repository = repository

    async def run_if_due(self) -> None:
        await self.repository.record_engine_event("info", "retention", "retention_checked", {})

