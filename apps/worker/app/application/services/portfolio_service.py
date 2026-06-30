from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.application.ports.repository_port import RepositoryPort
from app.domain.common.errors import ProviderError
from app.domain.portfolio.entities import Position


class PortfolioReadPort(Protocol):
    async def get_positions(self, now: datetime) -> list[Position]: ...


class PortfolioService:
    def __init__(
        self,
        repository: RepositoryPort,
        portfolio_reader: PortfolioReadPort,
    ) -> None:
        self.repository = repository
        self.portfolio_reader = portfolio_reader

    async def sync_positions(self, now: datetime) -> None:
        try:
            positions = await self.portfolio_reader.get_positions(now)
        except ProviderError as exc:
            await self.repository.record_engine_event(
                "warning",
                "portfolio",
                "portfolio_sync_failed",
                {"provider": exc.provider, "reason": exc.safe_message},
            )
            return
        await self.repository.replace_positions(positions, now)
