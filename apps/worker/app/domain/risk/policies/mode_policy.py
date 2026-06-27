from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class ModePolicy:
    name = "mode_live"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.settings.mode != "live":
            return block(self.name, "mode_not_live")
        return allow(self.name)
