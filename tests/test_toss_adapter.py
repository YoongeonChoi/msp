from __future__ import annotations

import unittest

from msp.adapters.toss import TossApiError, TossBrokerAdapter
from msp.domain.enums import OrderSide
from msp.domain.models import BrokerOrderCommand


class FakeTossBroker(TossBrokerAdapter):
    def __init__(self):
        super().__init__(
            base_url="https://openapi.tossinvest.com",
            client_id="client",
            client_secret="secret",
            account_seq="1",
        )
        self.calls = []
        self.error: Exception | None = None

    def _request(self, method, path, *, account_required=False, json_body=None, params=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "account_required": account_required,
                "json_body": json_body,
                "params": params,
            }
        )
        if self.error:
            raise self.error
        return {"orderId": "toss-order-1", "clientOrderId": json_body["clientOrderId"]}


def command(**overrides) -> BrokerOrderCommand:
    data = {
        "account_id": "paper-main",
        "order_intent_id": "intent-1",
        "broker_client_order_id": "msp_0123456789abcdef",
        "symbol": "005930",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 70000,
        "currency": "KRW",
    }
    data.update(overrides)
    return BrokerOrderCommand(**data)


class TossAdapterTest(unittest.TestCase):
    def test_submit_order_uses_official_quantity_based_contract(self) -> None:
        broker = FakeTossBroker()
        result = broker.submit_order(command())

        self.assertEqual(result.status, "ACKNOWLEDGED")
        call = broker.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/api/v1/orders")
        self.assertTrue(call["account_required"])
        self.assertEqual(
            call["json_body"],
            {
                "clientOrderId": "msp_0123456789abcdef",
                "symbol": "005930",
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "quantity": "10",
                "price": "70000",
                "confirmHighValueOrder": False,
            },
        )

    def test_high_value_order_sets_confirmation_flag(self) -> None:
        broker = FakeTossBroker()
        broker.submit_order(command(quantity=2000, limit_price=70000))
        self.assertTrue(broker.calls[0]["json_body"]["confirmHighValueOrder"])

    def test_fractional_quantity_is_rejected_before_network(self) -> None:
        broker = FakeTossBroker()
        result = broker.submit_order(command(quantity=1.5))
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(broker.calls, [])

    def test_request_in_progress_maps_to_unknown(self) -> None:
        broker = FakeTossBroker()
        broker.error = TossApiError(409, "request-in-progress", "same clientOrderId is in progress")
        result = broker.submit_order(command())
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.raw_response["code"], "request-in-progress")


if __name__ == "__main__":
    unittest.main()
