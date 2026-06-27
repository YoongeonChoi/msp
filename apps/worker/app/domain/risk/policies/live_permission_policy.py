from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class LivePermissionPolicy:
    name = "live_permission"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if not risk_input.settings.live_order_allowed:
            return block(self.name, "live_order_allowed_false")
        if risk_input.shutdown_requested:
            return block(self.name, "shutdown_requested")
        return allow(self.name)
