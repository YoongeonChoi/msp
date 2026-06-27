from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class LiquidityPolicy:
    name = "liquidity"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.liquidity_ok is not True:
            return block(self.name, "liquidity_unknown_or_insufficient", severity="high")
        return allow(self.name)
