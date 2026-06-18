from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from msp.domain.enums import OrderSide
from msp.domain.models import BrokerOrderCommand, BrokerOrderResult
from msp.exceptions import SafetyError
from msp.settings import Settings


class TossApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass
class _Token:
    access_token: str
    expires_at: float


class TossBrokerAdapter:
    """Official Toss Securities REST adapter.

    This adapter is deliberately thin. The execution engine still owns
    idempotency, approvals, state checks, and UNKNOWN handling.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        account_seq: str,
        timeout_seconds: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = str(account_seq)
        self.timeout_seconds = timeout_seconds
        self._token: _Token | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "TossBrokerAdapter":
        missing = [
            name
            for name, value in {
                "TOSSINVEST_CLIENT_ID": settings.toss_client_id,
                "TOSSINVEST_CLIENT_SECRET": settings.toss_client_secret,
                "TOSSINVEST_ACCOUNT_SEQ": settings.toss_account_seq,
            }.items()
            if not value
        ]
        if missing:
            raise SafetyError(f"Toss broker is not configured: missing {', '.join(missing)}")
        return cls(
            base_url=settings.toss_base_url,
            client_id=settings.toss_client_id or "",
            client_secret=settings.toss_client_secret or "",
            account_seq=settings.toss_account_seq or "",
            timeout_seconds=settings.toss_timeout_seconds,
        )

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderResult:
        validation_error = self._validate_quantity_based_order(command)
        if validation_error:
            return BrokerOrderResult(
                broker_order_id="",
                status="REJECTED",
                filled_quantity=0.0,
                average_price=0.0,
                raw_response={"broker": "toss", "reason": validation_error},
            )

        payload = {
            "clientOrderId": command.broker_client_order_id[:36],
            "symbol": command.symbol,
            "side": command.side.value,
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": self._whole_number(command.quantity),
            "price": self._decimal_string(command.limit_price),
            "confirmHighValueOrder": command.quantity * command.limit_price >= 100_000_000,
        }

        try:
            result = self._request("POST", "/api/v1/orders", account_required=True, json_body=payload)
        except TimeoutError as exc:
            return self._unknown(command, "timeout", str(exc))
        except urllib.error.URLError as exc:
            return self._unknown(command, "network-error", str(exc))
        except TossApiError as exc:
            if exc.status_code in {409, 429, 500} or exc.code == "request-in-progress":
                return self._unknown(command, exc.code, exc.message)
            return BrokerOrderResult(
                broker_order_id="",
                status="REJECTED",
                filled_quantity=0.0,
                average_price=0.0,
                raw_response={
                    "broker": "toss",
                    "code": exc.code,
                    "message": exc.message,
                    "data": exc.data,
                },
            )

        return BrokerOrderResult(
            broker_order_id=str(result.get("orderId", "")),
            status="ACKNOWLEDGED",
            filled_quantity=0.0,
            average_price=0.0,
            raw_response={"broker": "toss", "request": payload, "response": result},
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        try:
            result = self._request(
                "POST",
                f"/api/v1/orders/{urllib.parse.quote(broker_order_id)}/cancel",
                account_required=True,
                json_body={},
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            return BrokerOrderResult(
                broker_order_id=broker_order_id,
                status="UNKNOWN",
                filled_quantity=0.0,
                average_price=0.0,
                raw_response={"broker": "toss", "reason": str(exc)},
            )
        except TossApiError as exc:
            return BrokerOrderResult(
                broker_order_id=broker_order_id,
                status="REJECTED",
                filled_quantity=0.0,
                average_price=0.0,
                raw_response={"broker": "toss", "code": exc.code, "message": exc.message},
            )
        return BrokerOrderResult(
            broker_order_id=str(result.get("orderId", broker_order_id)),
            status="CANCELED",
            filled_quantity=0.0,
            average_price=0.0,
            raw_response={"broker": "toss", "response": result},
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        result = self._request(
            "GET",
            f"/api/v1/orders/{urllib.parse.quote(broker_order_id)}",
            account_required=True,
        )
        return self._result_from_order(result)

    def list_positions(self, account_id: str) -> list[dict]:
        result = self._request("GET", "/api/v1/holdings", account_required=True)
        items = result.get("items", []) if isinstance(result, dict) else []
        positions = []
        for item in items:
            positions.append(
                {
                    "account_id": account_id,
                    "symbol": item.get("symbol"),
                    "quantity": float(item.get("quantity", 0) or 0),
                    "avg_cost": float(item.get("averagePurchasePrice", 0) or 0),
                    "market_price": float(item.get("lastPrice", 0) or 0),
                    "currency": item.get("currency"),
                    "updated_at": None,
                }
            )
        return positions

    def list_cash(self, account_id: str) -> list[dict]:
        rows = []
        for currency in ("KRW", "USD"):
            try:
                result = self._request(
                    "GET",
                    "/api/v1/buying-power",
                    account_required=True,
                    params={"currency": currency},
                )
            except TossApiError:
                continue
            rows.append(
                {
                    "account_id": account_id,
                    "currency": currency,
                    "balance": float(result.get("cashBuyingPower", 0) or 0),
                    "updated_at": None,
                }
            )
        return rows

    def _token_value(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at - 60 > now:
            return self._token.access_token

        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/oauth2/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self._token = _Token(
            access_token=payload["access_token"],
            expires_at=now + int(payload.get("expires_in", 3600)),
        )
        return self._token.access_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        account_required: bool = False,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        headers = {"Authorization": f"Bearer {self._token_value()}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if account_required:
            headers["X-Tossinvest-Account"] = self.account_seq

        request = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc) from exc

        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def _api_error(self, exc: urllib.error.HTTPError) -> TossApiError:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            return TossApiError(exc.code, "http-error", str(exc))
        error = payload.get("error", payload)
        return TossApiError(
            exc.code,
            str(error.get("code", error.get("error", "http-error"))),
            str(error.get("message", error.get("error_description", str(exc)))),
            error.get("data") if isinstance(error, dict) else {},
        )

    def _unknown(self, command: BrokerOrderCommand, code: str, message: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            broker_order_id="",
            status="UNKNOWN",
            filled_quantity=0.0,
            average_price=0.0,
            raw_response={
                "broker": "toss",
                "clientOrderId": command.broker_client_order_id,
                "code": code,
                "message": message,
            },
        )

    def _result_from_order(self, order: dict) -> BrokerOrderResult:
        execution = order.get("execution") or {}
        status = self._map_order_status(str(order.get("status", "UNKNOWN")))
        return BrokerOrderResult(
            broker_order_id=str(order.get("orderId", "")),
            status=status,
            filled_quantity=float(execution.get("filledQuantity", 0) or 0),
            average_price=float(execution.get("averageFilledPrice", 0) or 0),
            raw_response={"broker": "toss", "response": order},
        )

    def _map_order_status(self, status: str) -> str:
        return {
            "PENDING": "ACKNOWLEDGED",
            "PENDING_CANCEL": "ACKNOWLEDGED",
            "PENDING_REPLACE": "ACKNOWLEDGED",
            "PARTIAL_FILLED": "PARTIALLY_FILLED",
            "FILLED": "FILLED",
            "CANCELED": "CANCELED",
            "REJECTED": "REJECTED",
            "CANCEL_REJECTED": "REJECTED",
            "REPLACE_REJECTED": "REJECTED",
            "REPLACED": "ACKNOWLEDGED",
        }.get(status, "UNKNOWN")

    def _validate_quantity_based_order(self, command: BrokerOrderCommand) -> str | None:
        if command.side not in {OrderSide.BUY, OrderSide.SELL}:
            return "invalid side"
        if command.quantity <= 0:
            return "quantity must be positive"
        if command.limit_price <= 0:
            return "limit price must be positive"
        if command.quantity != int(command.quantity):
            return "Toss quantity-based orders require a whole-share quantity"
        return None

    def _whole_number(self, value: float) -> str:
        return str(int(value))

    def _decimal_string(self, value: float) -> str:
        decimal_value = Decimal(str(value)).normalize()
        return format(decimal_value, "f")
