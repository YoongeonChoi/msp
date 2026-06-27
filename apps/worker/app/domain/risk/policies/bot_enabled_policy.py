from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class BotEnabledPolicy:
    name = "bot_enabled"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if not risk_input.settings.enabled:
            return block(self.name, "bot_disabled")
        return allow(self.name)
