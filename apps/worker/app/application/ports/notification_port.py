from __future__ import annotations

from typing import Protocol


class NotificationPort(Protocol):
    async def notify_warning(self, message: str, payload: dict[str, object]) -> None:
        ...

