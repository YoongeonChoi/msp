from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from app.application.ports.repository_port import RepositoryPort
from app.application.services.portfolio_service import PortfolioReadPort, PortfolioService
from app.domain.common.errors import ProviderUnavailableError
from app.domain.portfolio.entities import Position

NOW = datetime(2026, 6, 28, 1, 0, tzinfo=UTC)


async def test_portfolio_service_replaces_positions() -> None:
    repository = FakePortfolioRepository()
    reader = FakePortfolioReader(
        [
            Position(
                symbol="005930",
                quantity=2,
                avg_price_krw=65_000,
                current_price_krw=72_000,
                sector="unknown",
            )
        ]
    )

    await PortfolioService(
        cast(RepositoryPort, repository),
        cast(PortfolioReadPort, reader),
    ).sync_positions(NOW)

    assert repository.positions[0].symbol == "005930"
    assert repository.synced_at == NOW
    assert repository.events == []


async def test_portfolio_service_records_warning_on_provider_failure() -> None:
    repository = FakePortfolioRepository()

    await PortfolioService(
        cast(RepositoryPort, repository),
        cast(PortfolioReadPort, FailingPortfolioReader()),
    ).sync_positions(NOW)

    assert repository.positions == []
    assert repository.events == [
        {
            "level": "warning",
            "component": "portfolio",
            "message": "portfolio_sync_failed",
            "details": {
                "provider": "toss",
                "reason": "toss_positions_unavailable",
            },
        }
    ]


class FakePortfolioRepository:
    def __init__(self) -> None:
        self.positions: list[Position] = []
        self.synced_at: datetime | None = None
        self.events: list[dict[str, object]] = []

    async def replace_positions(
        self,
        positions: list[Position],
        synced_at: datetime,
    ) -> None:
        self.positions = positions
        self.synced_at = synced_at

    async def record_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        self.events.append(
            {
                "level": level,
                "component": component,
                "message": message,
                "details": details,
            }
        )


class FakePortfolioReader:
    def __init__(self, positions: list[Position]) -> None:
        self.positions = positions

    async def get_positions(self, now: datetime) -> list[Position]:
        return self.positions


class FailingPortfolioReader:
    async def get_positions(self, now: datetime) -> list[Position]:
        raise ProviderUnavailableError("toss", "toss_positions_unavailable")
