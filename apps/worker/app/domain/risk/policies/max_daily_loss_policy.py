from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxDailyLossPolicy:
    name = "max_daily_loss"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        account = risk_input.account_state
        if account is None:
            return block(self.name, "missing_account_state")
        if account.daily_loss_pct >= risk_input.settings.max_daily_loss_pct:
            return block(self.name, "max_daily_loss_pct_exceeded")
        return allow(self.name)
