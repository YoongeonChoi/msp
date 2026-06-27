from __future__ import annotations

from typing import Protocol

from app.domain.fundamentals.entities import QuarterlyFundamentals


class FundamentalsPort(Protocol):
    async def get_latest(self, symbol: str) -> QuarterlyFundamentals | None:
        ...

    async def provider_health(self) -> bool:
        ...

