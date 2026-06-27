from app.domain.common.time import age_seconds
from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput


class QuoteFreshnessPolicy:
    name = "quote_freshness"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        quote = risk_input.quote
        if quote is None:
            return block(self.name, "missing_quote")
        if age_seconds(quote.as_of, risk_input.now) > risk_input.settings.quote_freshness_sec:
            return block(self.name, "stale_quote")
        if quote.price_krw <= 0:
            return block(self.name, "invalid_quote_price")
        return allow(self.name)
