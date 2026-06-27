from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxOrderAmountPolicy:
    name = "max_order_amount"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.signal.order_amount_krw > risk_input.settings.max_order_amount_krw:
            return block(self.name, "max_order_amount_krw_exceeded", severity="high")
        if risk_input.signal.order_amount_krw <= 0:
            return block(self.name, "invalid_order_amount")
        return allow(self.name)
