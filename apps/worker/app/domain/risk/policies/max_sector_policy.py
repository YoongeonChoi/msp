from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class MaxSectorPolicy:
    name = "max_sector"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.sector_position_pct >= risk_input.settings.max_sector_pct:
            return block(self.name, "max_sector_pct_exceeded", severity="high")
        return allow(self.name)
