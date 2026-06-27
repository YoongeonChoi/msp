from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class DuplicateOrderPolicy:
    name = "duplicate_order"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.duplicate_order:
            return block(self.name, "duplicate_order_detected")
        return allow(self.name)
