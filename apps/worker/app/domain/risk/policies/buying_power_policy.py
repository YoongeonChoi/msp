from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.order_calculations import calculate_order_notional


class BuyingPowerPolicy:
    name = "buying_power"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.signal.action != "buy":
            return allow(self.name)
        account = risk_input.account_state
        if account is None or account.cash_krw < 0:
            return block(self.name, "cash_buying_power_unknown", severity="high")
        quote = risk_input.quote
        if quote is None:
            return block(self.name, "order_quantity_invalid", severity="high")
        order_notional = calculate_order_notional(
            risk_input.signal.order_amount_krw,
            quote.price_krw,
        )
        if order_notional is None:
            return block(self.name, "order_quantity_invalid", severity="high")
        if account.cash_krw < order_notional:
            return block(self.name, "insufficient_cash_buying_power", severity="high")
        return allow(self.name)
