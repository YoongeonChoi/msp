from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApiHealthSnapshot:
    provider: str
    healthy: bool
    details: dict[str, object]

