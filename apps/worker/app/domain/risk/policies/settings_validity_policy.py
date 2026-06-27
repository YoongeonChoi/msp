from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.policies import settings_validation_reasons


class SettingsValidityPolicy:
    name = "settings_validity"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        reasons = settings_validation_reasons(risk_input.settings)
        if reasons:
            return block(self.name, ",".join(reasons))
        return allow(self.name)
