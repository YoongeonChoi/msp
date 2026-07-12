from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxPositionPolicy:
    name = "max_position"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.signal.action != "buy":
            return allow(self.name)
        if risk_input.existing_position_pct is None:
            return block(self.name, "position_exposure_unknown", severity="high")
        account = risk_input.account_state
        if account is None or account.equity_krw <= 0:
            return block(self.name, "position_exposure_equity_invalid", severity="high")
        projected_position_pct = (
            risk_input.existing_position_pct
            + (risk_input.signal.order_amount_krw / account.equity_krw)
        )
        if projected_position_pct > risk_input.settings.max_position_pct:
            return block(self.name, "max_position_pct_exceeded", severity="high")
        return allow(self.name)
