from __future__ import annotations

from app.domain.risk.entities import PolicyResult, RiskResult
from app.domain.risk.policies import (
    AccountSyncPolicy,
    BotEnabledPolicy,
    CooldownPolicy,
    DuplicateOrderPolicy,
    LiquidityPolicy,
    LivePermissionPolicy,
    MarketOpenPolicy,
    MaxDailyLossPolicy,
    MaxDailyOrderCountPolicy,
    MaxOrderAmountPolicy,
    MaxPositionPolicy,
    MaxSectorPolicy,
    ModePolicy,
    NewsCriticalPolicy,
    ProviderHealthPolicy,
    QuoteFreshnessPolicy,
    VolatilityPolicy,
)
from app.domain.risk.policies.base import RiskPolicy
from app.domain.risk.value_objects import RiskInput


class RiskService:
    def __init__(self, policies: list[RiskPolicy] | None = None) -> None:
        self.policies = policies or [
            BotEnabledPolicy(),
            ModePolicy(),
            LivePermissionPolicy(),
            MarketOpenPolicy(),
            QuoteFreshnessPolicy(),
            AccountSyncPolicy(),
            ProviderHealthPolicy(),
            MaxPositionPolicy(),
            MaxSectorPolicy(),
            MaxDailyLossPolicy(),
            MaxDailyOrderCountPolicy(),
            MaxOrderAmountPolicy(),
            DuplicateOrderPolicy(),
            NewsCriticalPolicy(),
            LiquidityPolicy(),
            VolatilityPolicy(),
            CooldownPolicy(),
        ]

    def evaluate_live_order(self, risk_input: RiskInput) -> RiskResult:
        policy_results = [policy.evaluate(risk_input) for policy in self.policies]
        return self._aggregate(policy_results)

    def _aggregate(self, policy_results: list[PolicyResult]) -> RiskResult:
        blocked = [result for result in policy_results if not result.allowed]
        severity_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        severity = max(policy_results, key=lambda item: severity_rank[item.severity]).severity
        reasons = [result.reason for result in blocked]
        allowed = not blocked
        safe_message = "allowed" if allowed else "blocked:" + ",".join(reasons)
        return RiskResult(
            allowed=allowed,
            severity=severity,
            reasons=reasons,
            policy_results=policy_results,
            safe_message=safe_message,
        )

