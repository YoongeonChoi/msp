from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

CircuitState = Literal["closed", "open", "half_open"]


@dataclass(slots=True)
class CircuitBreaker:
    provider: str
    failure_threshold: int = 3
    recovery_seconds: int = 60
    state: CircuitState = "closed"
    failures: int = 0
    opened_at: datetime | None = None

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self, now: datetime) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = now

    def can_attempt(self, now: datetime) -> bool:
        if self.state == "closed":
            return True
        if self.opened_at and now - self.opened_at >= timedelta(seconds=self.recovery_seconds):
            self.state = "half_open"
            return True
        return False

