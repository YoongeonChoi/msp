from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.domain.common.errors import (
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.trading.entities import AccountState

ContractPlacementOutcome = Literal[
    "sent",
    "filled",
    "rejected",
    "timeout_before_accept",
    "timeout_after_accept",
    "http_5xx",
    "malformed",
    "unknown_after_accept",
]
ContractStatusOutcome = Literal[
    "open",
    "partial_filled",
    "filled",
    "rejected",
    "canceled",
    "timeout",
    "http_5xx",
    "malformed",
    "unknown",
    "duplicate",
    "mismatched_identity",
]
ContractCancelOutcome = Literal[
    "canceled",
    "timeout_before_accept",
    "timeout_after_accept",
    "http_5xx",
    "malformed",
    "unknown_after_accept",
    "duplicate",
]

QUALIFIED_TOSS_OPENAPI_SHA256 = (
    "2c54ebfd038a8c135f4b7f9036c42934d8ab9906c026251a7ae827b81e8e6aa8"
)


@dataclass(slots=True)
class _ContractOrder:
    request: BrokerOrderRequest
    status: Literal["sent", "partial_filled", "filled", "rejected", "canceled"]
    cumulative_quantity: int
    placement_result: BrokerOrderResult
    last_status_result: BrokerOrderStatusResult | None = None
    cancel_result: BrokerCancelOrderResult | None = None
    execution_sequence: int = 0


class ContractTestBroker:
    """Zero-network deterministic state machine for contract qualification drills."""

    execution_environment: Literal["contract_test"] = "contract_test"
    network_enabled = False
    production_order_capable = False

    def __init__(
        self,
        placement_outcomes: Iterable[ContractPlacementOutcome] = (),
        *,
        status_outcomes: Iterable[ContractStatusOutcome] = (),
        cancel_outcomes: Iterable[ContractCancelOutcome] = (),
        contract_artifact_sha256: str = QUALIFIED_TOSS_OPENAPI_SHA256,
    ) -> None:
        self._placement_outcomes = deque(placement_outcomes)
        self._status_outcomes = deque(status_outcomes)
        self._cancel_outcomes = deque(cancel_outcomes)
        self._orders: dict[str, _ContractOrder] = {}
        self._provider_order_ids_by_key: dict[str, str] = {}
        self._next_order_number = 1
        self.contract_artifact_sha256 = contract_artifact_sha256
        self.place_order_calls = 0
        self.get_order_status_calls = 0
        self.cancel_order_calls = 0

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self._require_qualified_contract_artifact()
        self.place_order_calls += 1
        self._validate_request(request)
        existing_id = self._provider_order_ids_by_key.get(request.idempotency_key)
        if existing_id is not None:
            existing = self._orders[existing_id]
            if existing.request != request:
                raise ProviderSchemaError(
                    "contract_test",
                    "contract_test_idempotency_payload_conflict",
                )
            return existing.placement_result

        outcome = self._placement_outcomes.popleft() if self._placement_outcomes else "sent"
        if outcome == "timeout_before_accept":
            raise ProviderTimeoutError("contract_test", "contract_test_timeout_before_accept")
        if outcome == "http_5xx":
            raise ProviderUnavailableError("contract_test", "contract_test_http_5xx")
        if outcome == "malformed":
            raise ProviderSchemaError("contract_test", "contract_test_malformed_create_response")

        provider_order_id = f"contract-{self._next_order_number:08d}"
        self._next_order_number += 1
        status: Literal["sent", "filled", "rejected"]
        if outcome == "filled":
            status = "filled"
        elif outcome == "rejected":
            status = "rejected"
        else:
            status = "sent"
        cumulative_quantity = request.quantity if status == "filled" else 0
        result = BrokerOrderResult(
            provider_order_id=provider_order_id,
            status=status,
            raw_summary=self._placement_summary(
                status=status,
                cumulative_quantity=cumulative_quantity,
                request=request,
                provider_order_id=provider_order_id,
            ),
        )
        self._orders[provider_order_id] = _ContractOrder(
            request=request,
            status=status,
            cumulative_quantity=cumulative_quantity,
            placement_result=result,
        )
        self._provider_order_ids_by_key[request.idempotency_key] = provider_order_id
        if outcome == "timeout_after_accept":
            raise ProviderTimeoutError("contract_test", "contract_test_timeout_after_accept")
        if outcome == "unknown_after_accept":
            raise ProviderUnknownError("contract_test", "contract_test_unknown_after_accept")
        return result

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        self._require_qualified_contract_artifact()
        self.get_order_status_calls += 1
        order = self._orders.get(provider_order_id)
        if order is None:
            raise ProviderUnavailableError("contract_test", "contract_test_order_not_found")
        outcome = self._status_outcomes.popleft() if self._status_outcomes else _status_outcome(
            order.status
        )
        if outcome == "timeout":
            raise ProviderTimeoutError("contract_test", "contract_test_status_timeout")
        if outcome == "http_5xx":
            raise ProviderUnavailableError("contract_test", "contract_test_status_http_5xx")
        if outcome == "malformed":
            raise ProviderSchemaError("contract_test", "contract_test_malformed_status_response")
        if outcome == "duplicate":
            if order.last_status_result is None:
                raise ProviderSchemaError(
                    "contract_test",
                    "contract_test_duplicate_status_without_predecessor",
                )
            return order.last_status_result
        if outcome == "unknown":
            result = BrokerOrderStatusResult(
                provider_order_id=provider_order_id,
                status="unknown_requires_manual_check",
                reason="contract_test_status_unknown",
                raw_summary={"contract_status": "unknown"},
            )
            order.last_status_result = result
            return result

        response_order_id = (
            f"mismatch-{provider_order_id}"
            if outcome == "mismatched_identity"
            else provider_order_id
        )
        normalized: Literal["sent", "partial_filled", "filled", "rejected", "canceled"]
        if outcome in {"open", "mismatched_identity"}:
            normalized = "sent"
        elif outcome in {"partial_filled", "filled", "rejected", "canceled"}:
            normalized = outcome
        else:  # pragma: no cover - guarded by the exhaustive fault branches above
            raise ProviderSchemaError(
                "contract_test",
                "contract_test_unhandled_status_outcome",
            )
        if normalized == "partial_filled":
            if order.request.quantity < 2:
                raise ProviderSchemaError(
                    "contract_test",
                    "contract_test_partial_fill_requires_multiple_shares",
                )
            order.cumulative_quantity = max(1, order.request.quantity // 2)
        elif normalized == "filled":
            order.cumulative_quantity = order.request.quantity
        elif normalized in {"rejected", "canceled"}:
            if order.cumulative_quantity:
                normalized = "canceled" if normalized == "canceled" else "rejected"
        order.status = normalized
        order.execution_sequence += 1
        provider_execution_id = (
            f"{provider_order_id}-execution-{order.execution_sequence}"
            if normalized in {"partial_filled", "filled"}
            else None
        )
        result = BrokerOrderStatusResult(
            provider_order_id=response_order_id,
            status=normalized,
            raw_summary={
                "contract_status": normalized,
                "cumulative_quantity": order.cumulative_quantity,
                "fill_price_krw": (
                    order.request.limit_price_krw if order.cumulative_quantity else None
                ),
                "provider_execution_id": provider_execution_id,
            },
        )
        order.last_status_result = result
        return result

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        self._require_qualified_contract_artifact()
        self.cancel_order_calls += 1
        order = self._orders.get(provider_order_id)
        if order is None:
            raise ProviderUnavailableError("contract_test", "contract_test_order_not_found")
        outcome = self._cancel_outcomes.popleft() if self._cancel_outcomes else "canceled"
        if outcome == "timeout_before_accept":
            raise ProviderTimeoutError(
                "contract_test",
                "contract_test_cancel_timeout_before_accept",
            )
        if outcome == "http_5xx":
            raise ProviderUnavailableError("contract_test", "contract_test_cancel_http_5xx")
        if outcome == "malformed":
            raise ProviderSchemaError("contract_test", "contract_test_malformed_cancel_response")
        if outcome == "duplicate":
            if order.cancel_result is None:
                raise ProviderSchemaError(
                    "contract_test",
                    "contract_test_duplicate_cancel_without_predecessor",
                )
            return order.cancel_result
        if order.status in {"filled", "rejected"}:
            raise ProviderUnavailableError("contract_test", "contract_test_order_not_cancelable")
        order.status = "canceled"
        result = BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id=f"cancel-{provider_order_id}",
            raw_summary={"contract_cancel_status": "accepted"},
        )
        order.cancel_result = result
        if outcome == "timeout_after_accept":
            raise ProviderTimeoutError("contract_test", "contract_test_cancel_timeout_after_accept")
        if outcome == "unknown_after_accept":
            raise ProviderUnknownError("contract_test", "contract_test_cancel_unknown_after_accept")
        return result

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=100_000_000,
            equity_krw=100_000_000,
            daily_loss_pct=0.0,
            daily_order_count=len(self._orders),
            synced_at=now,
        )

    @staticmethod
    def _validate_request(request: BrokerOrderRequest) -> None:
        if not request.symbol.strip() or not request.idempotency_key.strip():
            raise ProviderUnavailableError("contract_test", "contract_test_invalid_order_request")
        if (
            isinstance(request.quantity, bool)
            or request.quantity <= 0
            or isinstance(request.limit_price_krw, bool)
            or request.limit_price_krw <= 0
            or request.amount_krw != request.quantity * request.limit_price_krw
        ):
            raise ProviderUnavailableError("contract_test", "contract_test_invalid_order_request")

    def _require_qualified_contract_artifact(self) -> None:
        if self.contract_artifact_sha256 != QUALIFIED_TOSS_OPENAPI_SHA256:
            raise ProviderUnavailableError(
                "contract_test",
                "contract_test_artifact_hash_mismatch",
            )

    @staticmethod
    def _placement_summary(
        *,
        status: Literal["sent", "filled", "rejected"],
        cumulative_quantity: int,
        request: BrokerOrderRequest,
        provider_order_id: str,
    ) -> dict[str, object]:
        return {
            "contract_outcome": status,
            "cumulative_quantity": cumulative_quantity,
            "fill_price_krw": request.limit_price_krw if cumulative_quantity else None,
            "provider_execution_id": (
                f"{provider_order_id}-execution-1" if cumulative_quantity else None
            ),
        }


def _status_outcome(
    status: Literal["sent", "partial_filled", "filled", "rejected", "canceled"],
) -> ContractStatusOutcome:
    return "open" if status == "sent" else status
