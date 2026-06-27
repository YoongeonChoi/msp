from __future__ import annotations

from app.application.ports.repository_port import RepositoryPort


class HeartbeatRecorder:
    def __init__(self, repository: RepositoryPort) -> None:
        self.repository = repository

    async def ok(self, details: dict[str, object]) -> None:
        await self.repository.record_heartbeat("ok", details)

