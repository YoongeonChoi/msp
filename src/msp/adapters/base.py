from __future__ import annotations

from typing import Protocol

from msp.domain.models import BrokerOrderCommand, BrokerOrderResult


class BrokerAdapter(Protocol):
    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderResult:
        ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        ...

    def list_positions(self, account_id: str) -> list[dict]:
        ...

    def list_cash(self, account_id: str) -> list[dict]:
        ...

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        ...
