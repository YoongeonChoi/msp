from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MarketOpenPolicy:
    name = "market_open"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.market_open is not True:
            return block(self.name, "market_closed_or_unknown")
        return allow(self.name)
