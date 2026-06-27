from app.domain.common.time import age_seconds
from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class AccountSyncPolicy:
    name = "account_sync"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        account = risk_input.account_state
        if account is None or not account.synced:
            return block(self.name, "account_sync_missing_or_failed")
        if age_seconds(account.synced_at, risk_input.now) > 120:
            return block(self.name, "account_sync_stale")
        return allow(self.name)
