from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.common.json import JsonObject

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

OperationCommandType = Literal[
    "emergency_stop",
    "account_opening",
    "pause_paper",
    "resume_paper",
    "activate_paper_strategy",
    "start_contract_test",
    "apply_risk_policy_version",
]
WorkerHeartbeatStatus = Literal["ok", "warning", "error", "shutting_down"]
WORKER_APPLICABLE_OPERATION_COMMAND_TYPES = frozenset(
    {
        "emergency_stop",
        "account_opening",
        "pause_paper",
        "resume_paper",
        "activate_paper_strategy",
        "start_contract_test",
        "apply_risk_policy_version",
    }
)


class OperationsInvariantError(ValueError):
    """Raised when an operational contract is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class RecordedWorkerHeartbeat:
    heartbeat_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.heartbeat_id, "heartbeat_id")
        _require_aware(self.created_at, "heartbeat_created_at")


@dataclass(frozen=True, slots=True)
class ClaimedDeliveryOutboxItem:
    outbox_id: str
    dedupe_key: str
    event_type: str
    payload_version: int
    aggregate_type: str
    aggregate_id: str
    payload: JsonObject
    destination_type: str
    attempt_count: int
    lease_token: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.outbox_id, "outbox_id")
        _require_uuid(self.lease_token, "lease_token")
        for value, field in (
            (self.dedupe_key, "dedupe_key"),
            (self.event_type, "event_type"),
            (self.aggregate_type, "aggregate_type"),
            (self.aggregate_id, "aggregate_id"),
            (self.destination_type, "destination_type"),
        ):
            _require_text(value, field)
        _require_positive_int(self.payload_version, "payload_version")
        _require_positive_int(self.attempt_count, "attempt_count")
        _require_aware(self.lease_expires_at, "lease_expires_at")
        if not isinstance(self.payload, dict) or not all(
            isinstance(key, str) for key in self.payload
        ):
            raise OperationsInvariantError("outbox_payload_is_invalid")

@dataclass(frozen=True, slots=True)
class OutboxDeliveryReceipt:
    external_receipt_id: str
    external_receipt_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.external_receipt_id, "external_receipt_id")
        if _SHA256_RE.fullmatch(self.external_receipt_sha256) is None:
            raise OperationsInvariantError("external_receipt_sha256_is_invalid")


@dataclass(frozen=True, slots=True)
class CompletedOutboxDelivery:
    outbox_id: str
    status: Literal["delivered"]
    delivered_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.outbox_id, "outbox_id")
        if self.status != "delivered":
            raise OperationsInvariantError("outbox_completion_status_is_invalid")
        _require_aware(self.delivered_at, "delivered_at")


@dataclass(frozen=True, slots=True)
class FailedOutboxDelivery:
    outbox_id: str
    status: Literal["pending", "dead_letter"]
    available_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.outbox_id, "outbox_id")
        if self.status not in {"pending", "dead_letter"}:
            raise OperationsInvariantError("outbox_failure_status_is_invalid")
        _require_aware(self.available_at, "available_at")


@dataclass(frozen=True, slots=True)
class OperationCommandAcknowledgement:
    command_id: str
    state: Literal["claimed", "applied", "failed"]
    claimed_at: datetime | None
    applied_at: datetime | None
    post_control_epoch: int | None
    failure_code: str | None

    def __post_init__(self) -> None:
        _require_uuid(self.command_id, "command_id")
        if self.state not in {"claimed", "applied", "failed"}:
            raise OperationsInvariantError("operation_command_state_is_invalid")
        if self.claimed_at is not None:
            _require_aware(self.claimed_at, "claimed_at")
        if self.applied_at is not None:
            _require_aware(self.applied_at, "applied_at")


@dataclass(frozen=True, slots=True)
class ClaimedOperationCommand:
    command_id: str
    command_type: OperationCommandType
    environment: Literal["paper", "contract_test"]
    account_id: str
    requested_change: JsonObject
    requested_at: datetime
    expires_at: datetime
    revision: int
    claimed_at: datetime
    claim_expires_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.command_id, "command_id")
        if self.command_type not in WORKER_APPLICABLE_OPERATION_COMMAND_TYPES:
            raise OperationsInvariantError("operation_command_type_is_invalid")
        if self.environment not in {"paper", "contract_test"}:
            raise OperationsInvariantError("operation_command_environment_is_invalid")
        _require_text(self.account_id, "operation_command_account_id")
        if not isinstance(self.requested_change, dict) or not all(
            isinstance(key, str) for key in self.requested_change
        ):
            raise OperationsInvariantError("operation_requested_change_is_invalid")
        for value, field in (
            (self.requested_at, "operation_requested_at"),
            (self.expires_at, "operation_expires_at"),
            (self.claimed_at, "operation_claimed_at"),
            (self.claim_expires_at, "operation_claim_expires_at"),
        ):
            _require_aware(value, field)
        _require_positive_int(self.revision, "operation_command_revision")
        if not self.requested_at <= self.claimed_at < self.expires_at:
            raise OperationsInvariantError("operation_command_timeline_is_invalid")
        if self.claim_expires_at <= self.claimed_at:
            raise OperationsInvariantError("operation_command_claim_lease_is_invalid")


def _require_uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise OperationsInvariantError(f"{field}_is_invalid") from exc


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OperationsInvariantError(f"{field}_is_required")


def _require_positive_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperationsInvariantError(f"{field}_must_be_positive")


def _require_aware(value: object, field: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OperationsInvariantError(f"{field}_must_be_timezone_aware")
