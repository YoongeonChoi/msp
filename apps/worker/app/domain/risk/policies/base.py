from __future__ import annotations

from typing import Literal, Protocol

from app.domain.risk.entities import PolicyResult
from app.domain.risk.value_objects import RiskInput


class RiskPolicy(Protocol):
    name: str

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        ...


def allow(policy: str, reason: str = "ok") -> PolicyResult:
    return PolicyResult(policy=policy, allowed=True, severity="low", reason=reason)


def block(
    policy: str,
    reason: str,
    severity: Literal["low", "medium", "high", "critical"] = "critical",
) -> PolicyResult:
    if severity not in {"low", "medium", "high", "critical"}:
        raise ValueError("invalid severity")
    return PolicyResult(policy=policy, allowed=False, severity=severity, reason=reason)
