from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class ProviderHealthPolicy:
    name = "provider_health"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        critical = ["supabase", "toss"]
        bad = [name for name in critical if risk_input.provider_health.get(name) is not True]
        if bad:
            return block(self.name, "critical_provider_unhealthy:" + ",".join(bad))
        return allow(self.name)
