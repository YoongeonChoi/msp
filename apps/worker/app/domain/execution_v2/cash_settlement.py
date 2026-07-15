from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from app.domain.execution_v2.models import ExecutionInvariantError

CashSettlementObligationType = Literal["cash_payable", "cash_receivable"]
CashSettlementFailureCode = Literal[
    "settlement_dependency_unavailable",
    "settlement_projection_conflict",
    "settlement_worker_error",
]
CashSettlementFailureState = Literal["pending", "dead_letter"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CashSettlementCompletionAmbiguousError(ExecutionInvariantError):
    """The request may have committed; only exact completion replay is safe."""

    def __init__(self) -> None:
        super().__init__("cash_settlement_completion_result_is_ambiguous")


class CashSettlementCompletionRetryableError(ExecutionInvariantError):
    """The DB proved the completion transaction rolled back and may be retried."""

    def __init__(self, failure_code: CashSettlementFailureCode) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class CashSettlementClaim:
    obligation_id: str
    fill_id: str
    intent_id: str
    account_id: str
    environment: Literal["paper", "contract_test"]
    obligation_type: CashSettlementObligationType
    amount_krw: int
    settlement_date: date
    obligation_sha256: str
    revision: int
    claim_token: str
    claim_expires_at: datetime

    def __post_init__(self) -> None:
        for value, field in (
            (self.obligation_id, "cash_settlement_obligation_id"),
            (self.fill_id, "cash_settlement_fill_id"),
            (self.intent_id, "cash_settlement_intent_id"),
            (self.claim_token, "cash_settlement_claim_token"),
        ):
            _require_uuid(value, field)
        if not self.account_id.strip():
            raise ExecutionInvariantError("cash_settlement_account_id_is_required")
        if self.environment not in {"paper", "contract_test"}:
            raise ExecutionInvariantError("cash_settlement_environment_is_invalid")
        if self.obligation_type not in {"cash_payable", "cash_receivable"}:
            raise ExecutionInvariantError("cash_settlement_obligation_type_is_invalid")
        _require_positive_int(self.amount_krw, "cash_settlement_amount_krw")
        if type(self.settlement_date) is not date:
            raise ExecutionInvariantError("cash_settlement_date_is_invalid")
        if _SHA256_RE.fullmatch(self.obligation_sha256) is None:
            raise ExecutionInvariantError("cash_settlement_obligation_sha256_is_invalid")
        _require_positive_int(self.revision, "cash_settlement_revision")
        _require_aware(self.claim_expires_at, "cash_settlement_claim_expires_at")


@dataclass(frozen=True, slots=True)
class CashSettlementReceipt:
    obligation_id: str
    settlement_transaction_id: str
    claim_revision: int
    settled_revision: int
    obligation_type: CashSettlementObligationType
    amount_krw: int
    settlement_date: date
    settled_at: datetime
    replayed: bool

    def __post_init__(self) -> None:
        _require_uuid(self.obligation_id, "cash_settlement_receipt_obligation_id")
        _require_uuid(
            self.settlement_transaction_id,
            "cash_settlement_receipt_transaction_id",
        )
        _require_positive_int(self.claim_revision, "cash_settlement_claim_revision")
        if self.settled_revision != self.claim_revision + 1:
            raise ExecutionInvariantError("cash_settlement_settled_revision_is_invalid")
        if self.obligation_type not in {"cash_payable", "cash_receivable"}:
            raise ExecutionInvariantError("cash_settlement_receipt_type_is_invalid")
        _require_positive_int(self.amount_krw, "cash_settlement_receipt_amount_krw")
        if type(self.settlement_date) is not date:
            raise ExecutionInvariantError("cash_settlement_receipt_date_is_invalid")
        _require_aware(self.settled_at, "cash_settlement_receipt_settled_at")
        if not isinstance(self.replayed, bool):
            raise ExecutionInvariantError("cash_settlement_receipt_replayed_is_invalid")


@dataclass(frozen=True, slots=True)
class CashSettlementFailureReceipt:
    obligation_id: str
    claim_revision: int
    revision: int
    state: CashSettlementFailureState
    attempt_count: int
    available_at: datetime
    error_code: CashSettlementFailureCode
    replayed: bool

    def __post_init__(self) -> None:
        _require_uuid(self.obligation_id, "cash_settlement_failure_obligation_id")
        _require_positive_int(self.claim_revision, "cash_settlement_failure_claim_revision")
        if self.revision != self.claim_revision + 1:
            raise ExecutionInvariantError("cash_settlement_failure_revision_is_invalid")
        if self.state not in {"pending", "dead_letter"}:
            raise ExecutionInvariantError("cash_settlement_failure_state_is_invalid")
        _require_positive_int(self.attempt_count, "cash_settlement_attempt_count")
        if (self.state == "dead_letter") != (self.attempt_count >= 8):
            raise ExecutionInvariantError(
                "cash_settlement_failure_attempt_state_is_invalid"
            )
        _require_aware(self.available_at, "cash_settlement_available_at")
        if self.error_code not in {
            "settlement_dependency_unavailable",
            "settlement_projection_conflict",
            "settlement_worker_error",
        }:
            raise ExecutionInvariantError("cash_settlement_error_code_is_invalid")
        if not isinstance(self.replayed, bool):
            raise ExecutionInvariantError("cash_settlement_failure_replayed_is_invalid")


def _require_uuid(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionInvariantError(f"{field}_is_invalid") from exc
    if str(parsed) != value:
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_positive_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError(f"{field}_must_be_timezone_aware")
