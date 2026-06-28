from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxDailyOrderCountPolicy:
    name = "max_daily_order_count"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        account = risk_input.account_state
        if account is None:
            return block(self.name, "missing_account_state")
        if not account.daily_order_count_verified:
            return block(self.name, "daily_order_count_unverified", severity="high")
        if account.daily_order_count >= risk_input.settings.max_daily_order_count:
            return block(self.name, "max_daily_order_count_exceeded", severity="high")
        return allow(self.name)
