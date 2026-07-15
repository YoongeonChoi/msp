from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from app.domain.execution_v2.models import ExecutionInvariantError, ExecutionStatus

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_KRW_4DP_RE = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{4}$")

ReconciliationOutcome = Literal["reschedule", "complete", "manual"]
RecoveryDisposition = Literal[
    "same_release",
    "pre_dispatch_release_takeover",
    "manual_release_takeover",
]


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationClaim:
    intent_id: str
    attempt_id: str | None
    provider_order_id: str | None
    latest_observation_id: str | None
    latest_sequence: int | None
    latest_status: ExecutionStatus | None
    latest_observed_at: datetime | None
    latest_cumulative_quantity: int | None
    latest_cumulative_gross_krw: int | None
    latest_cumulative_commission_krw: int | None
    latest_cumulative_tax_krw: int | None
    observation_history_sha256: str | None
    environment: Literal["paper", "contract_test"]
    account_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    limit_price_krw: int
    semantic_key_sha256: str
    decision_id: str
    risk_result_id: str
    execution_policy_version: str
    cost_schedule_version: str
    risk_policy_sha256: str
    provider_contract_version: str | None
    provider_openapi_sha256: str | None
    position_cost_basis_method: Literal["moving_weighted_average_v1"] | None
    position_quantity_snapshot: int | None
    position_average_cost_krw: str | None
    position_total_cost_krw: int | None
    position_projection_version: int | None
    position_cost_basis_sha256: str | None
    lease_fencing_token: int
    reservation_fencing_token: int
    control_epoch: int
    reservation_control_epoch: int
    intent_release_sha: str
    lease_release_sha: str
    recovery_disposition: RecoveryDisposition
    eligible_at: datetime
    expires_at: datetime
    priority: int
    next_reconcile_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.intent_id, "reconciliation_intent_id")
        for value, field in (
            (self.attempt_id, "reconciliation_attempt_id"),
            (self.latest_observation_id, "reconciliation_latest_observation_id"),
        ):
            if value is not None:
                _require_uuid(value, field)
        for value, field in (
            (self.account_id, "reconciliation_account_id"),
            (self.symbol, "reconciliation_symbol"),
            (self.execution_policy_version, "reconciliation_execution_policy_version"),
            (self.cost_schedule_version, "reconciliation_cost_schedule_version"),
        ):
            _require_text(value, field)
        if self.provider_order_id is not None:
            _require_text(self.provider_order_id, "reconciliation_provider_order_id")
        if self.provider_contract_version is not None:
            _require_text(
                self.provider_contract_version,
                "reconciliation_provider_contract_version",
            )
        _require_positive_int(self.quantity, "reconciliation_quantity")
        _require_positive_int(
            self.limit_price_krw,
            "reconciliation_limit_price_krw",
        )
        _require_sha256(self.semantic_key_sha256, "reconciliation_semantic_key_sha256")
        _require_uuid(self.decision_id, "reconciliation_decision_id")
        _require_uuid(self.risk_result_id, "reconciliation_risk_result_id")
        _require_sha256(self.risk_policy_sha256, "reconciliation_risk_policy_sha256")
        if self.provider_openapi_sha256 is not None:
            _require_sha256(
                self.provider_openapi_sha256,
                "reconciliation_provider_openapi_sha256",
            )
        position_cost_basis = (
            self.position_cost_basis_method,
            self.position_quantity_snapshot,
            self.position_average_cost_krw,
            self.position_total_cost_krw,
            self.position_projection_version,
            self.position_cost_basis_sha256,
        )
        if self.side == "buy" and any(item is not None for item in position_cost_basis):
            raise ExecutionInvariantError(
                "reconciliation_buy_position_cost_basis_is_unexpected"
            )
        if self.side == "sell":
            if any(item is None for item in position_cost_basis):
                raise ExecutionInvariantError(
                    "reconciliation_sell_position_cost_basis_is_required"
                )
            assert self.position_quantity_snapshot is not None
            assert self.position_average_cost_krw is not None
            assert self.position_total_cost_krw is not None
            assert self.position_projection_version is not None
            assert self.position_cost_basis_sha256 is not None
            if self.position_cost_basis_method != "moving_weighted_average_v1":
                raise ExecutionInvariantError(
                    "reconciliation_position_cost_basis_method_is_invalid"
                )
            _require_positive_int(
                self.position_quantity_snapshot,
                "reconciliation_position_quantity_snapshot",
            )
            if self.position_quantity_snapshot < self.quantity:
                raise ExecutionInvariantError(
                    "reconciliation_position_quantity_is_insufficient"
                )
            average_cost = _require_positive_krw_4dp(
                self.position_average_cost_krw,
                "reconciliation_position_average_cost_krw",
            )
            _require_positive_int(
                self.position_total_cost_krw,
                "reconciliation_position_total_cost_krw",
            )
            expected_total_cost = int(
                (average_cost * self.position_quantity_snapshot).quantize(
                    Decimal("1"),
                    rounding=ROUND_HALF_UP,
                )
            )
            if self.position_total_cost_krw != expected_total_cost:
                raise ExecutionInvariantError(
                    "reconciliation_position_total_cost_is_invalid"
                )
            _require_positive_int(
                self.position_projection_version,
                "reconciliation_position_projection_version",
            )
            _require_sha256(
                self.position_cost_basis_sha256,
                "reconciliation_position_cost_basis_sha256",
            )
        _require_positive_int(
            self.lease_fencing_token,
            "reconciliation_lease_fencing_token",
        )
        _require_positive_int(
            self.reservation_fencing_token,
            "reconciliation_reservation_fencing_token",
        )
        if self.lease_fencing_token < self.reservation_fencing_token:
            raise ExecutionInvariantError(
                "reconciliation_current_fencing_token_is_stale"
            )
        _require_positive_int(self.control_epoch, "reconciliation_control_epoch")
        _require_positive_int(
            self.reservation_control_epoch,
            "reconciliation_reservation_control_epoch",
        )
        if self.control_epoch < self.reservation_control_epoch:
            raise ExecutionInvariantError("reconciliation_current_control_epoch_is_stale")
        for release_sha, field in (
            (self.intent_release_sha, "reconciliation_intent_release_sha"),
            (self.lease_release_sha, "reconciliation_lease_release_sha"),
        ):
            if _RELEASE_SHA_RE.fullmatch(release_sha) is None:
                raise ExecutionInvariantError(f"{field}_is_invalid")
        expected_disposition: RecoveryDisposition
        if self.intent_release_sha == self.lease_release_sha:
            expected_disposition = "same_release"
        elif self.attempt_id is None:
            expected_disposition = "pre_dispatch_release_takeover"
        else:
            expected_disposition = "manual_release_takeover"
        if self.recovery_disposition != expected_disposition:
            raise ExecutionInvariantError(
                "reconciliation_recovery_disposition_is_invalid"
            )
        latest = (
            self.latest_observation_id,
            self.latest_sequence,
            self.latest_status,
            self.latest_observed_at,
            self.latest_cumulative_quantity,
            self.latest_cumulative_gross_krw,
            self.latest_cumulative_commission_krw,
            self.latest_cumulative_tax_krw,
            self.observation_history_sha256,
        )
        if any(item is None for item in latest) and any(item is not None for item in latest):
            raise ExecutionInvariantError("reconciliation_latest_observation_is_partial")
        if self.latest_sequence is not None:
            _require_positive_int(
                self.latest_sequence,
                "reconciliation_latest_sequence",
            )
            assert self.latest_observed_at is not None
            _require_aware(
                self.latest_observed_at,
                "reconciliation_latest_observed_at",
            )
            for cumulative_value, cumulative_field in (
                (
                    self.latest_cumulative_quantity,
                    "reconciliation_latest_cumulative_quantity",
                ),
                (
                    self.latest_cumulative_gross_krw,
                    "reconciliation_latest_cumulative_gross_krw",
                ),
                (
                    self.latest_cumulative_commission_krw,
                    "reconciliation_latest_cumulative_commission_krw",
                ),
                (
                    self.latest_cumulative_tax_krw,
                    "reconciliation_latest_cumulative_tax_krw",
                ),
            ):
                _require_nonnegative_int(cumulative_value, cumulative_field)
            assert self.observation_history_sha256 is not None
            _require_sha256(
                self.observation_history_sha256,
                "reconciliation_observation_history_sha256",
            )
        if self.attempt_id is None and (
            self.provider_order_id is not None or self.latest_observation_id is not None
        ):
            raise ExecutionInvariantError(
                "reconciliation_pre_dispatch_evidence_is_inconsistent"
            )
        if self.latest_observation_id is not None and self.provider_order_id is None:
            raise ExecutionInvariantError(
                "reconciliation_latest_observation_requires_provider_order"
            )
        if (
            self.environment == "paper"
            and self.provider_order_id is not None
            and self.provider_order_id != f"paper:{self.intent_id}"
        ):
            raise ExecutionInvariantError(
                "reconciliation_paper_provider_order_identity_is_invalid"
            )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ExecutionInvariantError("reconciliation_priority_is_invalid")
        _require_aware(self.eligible_at, "reconciliation_eligible_at")
        _require_aware(self.expires_at, "reconciliation_expires_at")
        if self.expires_at < self.eligible_at:
            raise ExecutionInvariantError("reconciliation_execution_window_is_invalid")
        _require_aware(self.next_reconcile_at, "reconciliation_next_reconcile_at")
        _require_aware(self.lease_expires_at, "reconciliation_lease_expires_at")

    @property
    def cursor(self) -> tuple[int, str]:
        return self.priority, self.intent_id


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationCompletion:
    intent_id: str
    state: Literal["pending", "complete", "manual"]
    next_reconcile_at: datetime | None

    def __post_init__(self) -> None:
        _require_uuid(self.intent_id, "reconciliation_completion_intent_id")
        if self.state not in {"pending", "complete", "manual"}:
            raise ExecutionInvariantError("reconciliation_completion_state_is_invalid")
        if self.next_reconcile_at is not None:
            _require_aware(
                self.next_reconcile_at,
                "reconciliation_completion_next_reconcile_at",
            )
        if (self.state == "pending") != (self.next_reconcile_at is not None):
            raise ExecutionInvariantError("reconciliation_completion_schedule_is_invalid")


@dataclass(frozen=True, slots=True)
class PreDispatchFailureResult:
    intent_id: str
    observation_id: str
    state: Literal["complete"]
    reason_code: str
    idempotent: bool

    def __post_init__(self) -> None:
        _require_uuid(self.intent_id, "pre_dispatch_failure_intent_id")
        _require_uuid(self.observation_id, "pre_dispatch_failure_observation_id")
        if self.state != "complete":
            raise ExecutionInvariantError("pre_dispatch_failure_state_is_invalid")
        _require_text(self.reason_code, "pre_dispatch_failure_reason_code")
        if not isinstance(self.idempotent, bool):
            raise ExecutionInvariantError("pre_dispatch_failure_idempotent_is_invalid")


@dataclass(frozen=True, slots=True)
class ExpiredPaperIntentResult:
    intent_id: str
    observation_id: str
    sequence: int
    state: Literal["complete"]
    reason_code: str
    idempotent: bool

    def __post_init__(self) -> None:
        _require_uuid(self.intent_id, "expired_paper_intent_id")
        _require_uuid(self.observation_id, "expired_paper_observation_id")
        _require_positive_int(self.sequence, "expired_paper_observation_sequence")
        if self.state != "complete":
            raise ExecutionInvariantError("expired_paper_intent_state_is_invalid")
        _require_text(self.reason_code, "expired_paper_intent_reason_code")
        if not isinstance(self.idempotent, bool):
            raise ExecutionInvariantError("expired_paper_intent_idempotent_is_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationDecision:
    outcome: ReconciliationOutcome
    reason_code: str
    next_reconcile_at: datetime | None = None
    completion_persisted: bool = False

    def __post_init__(self) -> None:
        _require_text(self.reason_code, "reconciliation_reason_code")
        if self.next_reconcile_at is not None:
            _require_aware(self.next_reconcile_at, "reconciliation_decision_next_at")
        if (self.outcome == "reschedule") != (self.next_reconcile_at is not None):
            raise ExecutionInvariantError("reconciliation_decision_schedule_is_invalid")
        if self.completion_persisted and self.outcome not in {"complete", "manual"}:
            raise ExecutionInvariantError(
                "reconciliation_persisted_decision_must_be_terminal"
            )


def _require_uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionInvariantError(f"{field}_is_invalid") from exc


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError(f"{field}_is_required")


def _require_sha256(value: str, field: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_positive_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(f"{field}_must_be_positive")


def _require_nonnegative_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionInvariantError(f"{field}_must_be_nonnegative")


def _require_aware(value: object, field: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ExecutionInvariantError(f"{field}_must_be_timezone_aware")


def _require_positive_krw_4dp(value: str, field: str) -> Decimal:
    if _KRW_4DP_RE.fullmatch(value) is None:
        raise ExecutionInvariantError(f"{field}_is_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ExecutionInvariantError(f"{field}_is_invalid") from exc
    if parsed <= 0:
        raise ExecutionInvariantError(f"{field}_must_be_positive")
    return parsed
