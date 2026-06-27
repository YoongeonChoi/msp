from __future__ import annotations

from app.config import Settings
from app.domain.common.errors import ProviderAuthError


class TossAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def access_token(self) -> str:
        raise ProviderAuthError("toss", "toss_auth_flow_not_verified")

