from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class StrategyVersionPolicy:
    name = "strategy_version"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.strategy_version_id is None:
            return block(self.name, "missing_strategy_version")
        return allow(self.name)
