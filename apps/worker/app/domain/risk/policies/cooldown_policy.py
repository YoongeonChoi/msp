from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class CooldownPolicy:
    name = "cooldown"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.cooldown_active:
            return block(self.name, "symbol_in_cooldown", severity="high")
        return allow(self.name)
