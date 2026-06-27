from __future__ import annotations

from app.domain.common.errors import ProviderUnavailableError
from app.domain.fundamentals.entities import QuarterlyFundamentals


class OpenDartClient:
    async def provider_health(self) -> bool:
        return False

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals | None:
        raise ProviderUnavailableError("opendart", "opendart_contract_not_verified")
