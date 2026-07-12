from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class LiveStrategyApprovalPolicy:
    name = "live_strategy_approval"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.strategy_status != "active":
            return block(self.name, "live_strategy_not_active", severity="critical")
        if not risk_input.strategy_approved:
            return block(self.name, "live_strategy_not_approved", severity="critical")
        return allow(self.name)
