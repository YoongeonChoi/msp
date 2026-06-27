from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class VolatilityPolicy:
    name = "volatility"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.volatility_ok is not True:
            return block(self.name, "volatility_unknown_or_too_high", severity="high")
        return allow(self.name)
