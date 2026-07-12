from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class NewsCriticalPolicy:
    name = "news_critical"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.signal.action != "buy":
            return allow(self.name)
        if risk_input.critical_news_risk is None:
            return block(self.name, "critical_news_risk_unknown", severity="high")
        if risk_input.critical_news_risk:
            return block(self.name, "critical_negative_news_risk")
        return allow(self.name)
