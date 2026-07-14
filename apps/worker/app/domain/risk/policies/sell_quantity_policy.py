from app.domain.risk.entities import PolicyResult
from app.domain.risk.policies.base import allow, block
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.order_calculations import calculate_order_quantity


class SellQuantityPolicy:
    name = "sell_quantity"

    def evaluate(self, risk_input: RiskInput) -> PolicyResult:
        if risk_input.signal.action != "sell":
            return allow(self.name)
        quote = risk_input.quote
        if quote is None:
            return block(self.name, "order_quantity_invalid", severity="high")
        order_quantity = calculate_order_quantity(
            risk_input.signal.order_amount_krw,
            quote.price_krw,
        )
        if order_quantity is None:
            return block(self.name, "order_quantity_invalid", severity="high")
        available_quantity = risk_input.available_position_quantity
        if available_quantity is None or available_quantity < 0:
            return block(self.name, "sell_position_quantity_unknown", severity="high")
        if available_quantity < order_quantity:
            return block(self.name, "insufficient_sell_position_quantity", severity="high")
        return allow(self.name)
