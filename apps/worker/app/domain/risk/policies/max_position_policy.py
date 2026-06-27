from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxPositionPolicy:
    name = "max_position"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.existing_position_pct >= risk_input.settings.max_position_pct:
            return block(self.name, "max_position_pct_exceeded", severity="high")
        return allow(self.name)
