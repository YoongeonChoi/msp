from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

RiskSeverity = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True, slots=True)
class PolicyResult:
    policy: str
    allowed: bool
    severity: RiskSeverity
    reason: str


@dataclass(frozen=True, slots=True)
class RiskResult:
    allowed: bool
    severity: RiskSeverity
    reasons: list[str] = field(default_factory=list)
    policy_results: list[PolicyResult] = field(default_factory=list)
    safe_message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "severity": self.severity,
            "reasons": self.reasons,
            "policy_results": [asdict(result) for result in self.policy_results],
            "safe_message": self.safe_message,
        }
