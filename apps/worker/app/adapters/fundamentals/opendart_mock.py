from __future__ import annotations

from app.domain.fundamentals.entities import QuarterlyFundamentals


class OpenDartMock:
    async def provider_health(self) -> bool:
        return True

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals:
        return QuarterlyFundamentals(
            symbol=symbol,
            per=12.5,
            pbr=1.1,
            roe=0.12,
            operating_margin=0.15,
            debt_ratio=0.35,
        )

