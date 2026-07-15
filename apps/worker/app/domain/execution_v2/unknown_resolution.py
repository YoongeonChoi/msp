from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.execution_v2.models import (
    ExecutionEnvironment,
    ExecutionInvariantError,
)

UnknownResolutionTerminalStatus = Literal[
    "filled",
    "canceled",
    "expired",
    "rejected",
]
UnknownResolutionWorkState = Literal["approved", "claimed"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_ACCOUNT_BY_ENVIRONMENT: dict[ExecutionEnvironment, str] = {
    "paper": "paper-primary",
    "contract_test": "contract-test-primary",
}


class UnknownResolutionApplyAmbiguousError(RuntimeError):
    """The apply transaction may have committed, but no valid receipt was read."""


@dataclass(frozen=True, slots=True)
class UnknownResolutionCandidate:
    command_id: str
    request_id: str
    review_id: str
    break_id: str
    intent_id: str
    terminal_status: UnknownResolutionTerminalStatus
    request_payload_sha256: str
    review_payload_sha256: str
    command_revision: int
    work_revision: int
    work_state: UnknownResolutionWorkState
    claim_token: str | None
    claim_expires_at: datetime | None
    expected_control_epoch: int
    account_id: str
    environment: ExecutionEnvironment
    holder_id: str
    release_sha: str
    lease_fencing_token: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.command_id, "unknown_resolution_command_id"),
            (self.request_id, "unknown_resolution_request_id"),
            (self.review_id, "unknown_resolution_review_id"),
            (self.break_id, "unknown_resolution_break_id"),
            (self.intent_id, "unknown_resolution_intent_id"),
        ):
            _require_uuid(value, field)
        if self.command_id != self.request_id:
            raise ExecutionInvariantError("unknown_resolution_request_identity_mismatch")
        if self.terminal_status not in {
            "filled",
            "canceled",
            "expired",
            "rejected",
        }:
            raise ExecutionInvariantError("unknown_resolution_terminal_status_is_invalid")
        _require_sha256(
            self.request_payload_sha256,
            "unknown_resolution_request_payload_sha256",
        )
        _require_sha256(
            self.review_payload_sha256,
            "unknown_resolution_review_payload_sha256",
        )
        _require_nonnegative_int(
            self.command_revision,
            "unknown_resolution_command_revision",
        )
        _require_nonnegative_int(
            self.work_revision,
            "unknown_resolution_work_revision",
        )
        if self.work_state not in {"approved", "claimed"}:
            raise ExecutionInvariantError("unknown_resolution_work_state_is_invalid")
        if self.work_state == "approved":
            if self.claim_token is not None or self.claim_expires_at is not None:
                raise ExecutionInvariantError("unknown_resolution_approved_claim_is_invalid")
        else:
            if self.claim_expires_at is None:
                raise ExecutionInvariantError("unknown_resolution_claim_expiry_is_required")
            _require_aware(
                self.claim_expires_at,
                "unknown_resolution_claim_expires_at",
            )
            if self.claim_token is not None:
                _require_uuid(
                    self.claim_token,
                    "unknown_resolution_claim_token",
                )
        _require_positive_int(
            self.expected_control_epoch,
            "unknown_resolution_expected_control_epoch",
        )
        _require_scope(
            account_id=self.account_id,
            environment=self.environment,
            holder_id=self.holder_id,
            release_sha=self.release_sha,
            fencing_token=self.lease_fencing_token,
        )

    def has_active_owned_claim(self, now: datetime) -> bool:
        _require_aware(now, "unknown_resolution_claim_check_time")
        return (
            self.work_state == "claimed"
            and self.claim_token is not None
            and self.claim_expires_at is not None
            and now < self.claim_expires_at
        )


@dataclass(frozen=True, slots=True)
class UnknownResolutionClaim:
    command_id: str
    request_id: str
    review_id: str
    break_id: str
    intent_id: str
    terminal_status: UnknownResolutionTerminalStatus
    request_payload_sha256: str
    review_payload_sha256: str
    command_revision: int
    work_revision: int
    claim_token: str
    claim_expires_at: datetime
    expected_control_epoch: int
    account_id: str
    environment: ExecutionEnvironment
    holder_id: str
    release_sha: str
    lease_fencing_token: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.command_id, "unknown_resolution_claim_command_id"),
            (self.request_id, "unknown_resolution_claim_request_id"),
            (self.review_id, "unknown_resolution_claim_review_id"),
            (self.break_id, "unknown_resolution_claim_break_id"),
            (self.intent_id, "unknown_resolution_claim_intent_id"),
            (self.claim_token, "unknown_resolution_claim_token"),
        ):
            _require_uuid(value, field)
        if self.command_id != self.request_id:
            raise ExecutionInvariantError("unknown_resolution_claim_identity_mismatch")
        if self.terminal_status not in {
            "filled",
            "canceled",
            "expired",
            "rejected",
        }:
            raise ExecutionInvariantError("unknown_resolution_terminal_status_is_invalid")
        _require_sha256(
            self.request_payload_sha256,
            "unknown_resolution_claim_request_sha256",
        )
        _require_sha256(
            self.review_payload_sha256,
            "unknown_resolution_claim_review_sha256",
        )
        _require_nonnegative_int(
            self.command_revision,
            "unknown_resolution_claim_command_revision",
        )
        _require_nonnegative_int(
            self.work_revision,
            "unknown_resolution_claim_work_revision",
        )
        _require_aware(
            self.claim_expires_at,
            "unknown_resolution_claim_expires_at",
        )
        _require_positive_int(
            self.expected_control_epoch,
            "unknown_resolution_claim_control_epoch",
        )
        _require_scope(
            account_id=self.account_id,
            environment=self.environment,
            holder_id=self.holder_id,
            release_sha=self.release_sha,
            fencing_token=self.lease_fencing_token,
        )


@dataclass(frozen=True, slots=True)
class UnknownResolutionApplicationReceipt:
    schema_version: Literal[2]
    command_id: str
    break_id: str
    intent_id: str
    state: Literal["applied"]
    receipt_revision: int
    break_revision: int
    request_digest_sha256: str
    review_digest_sha256: str
    terminal_status: UnknownResolutionTerminalStatus
    claim_token: str
    work_revision: int
    application_id: str
    application_sha256: str
    accounting_mutation_allowed: Literal[True]
    resolution_complete: Literal[True]
    inserted: bool
    account_id: str
    environment: ExecutionEnvironment
    holder_id: str
    release_sha: str
    lease_fencing_token: int
    control_epoch: int

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ExecutionInvariantError("unknown_resolution_receipt_version_is_invalid")
        for value, field in (
            (self.command_id, "unknown_resolution_receipt_command_id"),
            (self.break_id, "unknown_resolution_receipt_break_id"),
            (self.intent_id, "unknown_resolution_receipt_intent_id"),
            (self.claim_token, "unknown_resolution_receipt_claim_token"),
            (self.application_id, "unknown_resolution_receipt_application_id"),
        ):
            _require_uuid(value, field)
        if self.state != "applied":
            raise ExecutionInvariantError("unknown_resolution_receipt_state_is_invalid")
        _require_nonnegative_int(
            self.receipt_revision,
            "unknown_resolution_receipt_revision",
        )
        _require_nonnegative_int(
            self.break_revision,
            "unknown_resolution_receipt_break_revision",
        )
        _require_nonnegative_int(
            self.work_revision,
            "unknown_resolution_receipt_work_revision",
        )
        for value, field in (
            (
                self.request_digest_sha256,
                "unknown_resolution_receipt_request_sha256",
            ),
            (
                self.review_digest_sha256,
                "unknown_resolution_receipt_review_sha256",
            ),
            (
                self.application_sha256,
                "unknown_resolution_receipt_application_sha256",
            ),
        ):
            _require_sha256(value, field)
        if self.terminal_status not in {
            "filled",
            "canceled",
            "expired",
            "rejected",
        }:
            raise ExecutionInvariantError("unknown_resolution_terminal_status_is_invalid")
        if self.accounting_mutation_allowed is not True:
            raise ExecutionInvariantError(
                "unknown_resolution_accounting_mutation_was_not_confirmed"
            )
        if self.resolution_complete is not True:
            raise ExecutionInvariantError("unknown_resolution_was_not_completed")
        if not isinstance(self.inserted, bool):
            raise ExecutionInvariantError("unknown_resolution_inserted_flag_is_invalid")
        _require_positive_int(
            self.control_epoch,
            "unknown_resolution_receipt_control_epoch",
        )
        _require_scope(
            account_id=self.account_id,
            environment=self.environment,
            holder_id=self.holder_id,
            release_sha=self.release_sha,
            fencing_token=self.lease_fencing_token,
        )

    @property
    def replayed(self) -> bool:
        return not self.inserted


def _require_scope(
    *,
    account_id: str,
    environment: ExecutionEnvironment,
    holder_id: str,
    release_sha: str,
    fencing_token: int,
) -> None:
    if _ACCOUNT_BY_ENVIRONMENT.get(environment) != account_id:
        raise ExecutionInvariantError("unknown_resolution_account_environment_mismatch")
    _require_uuid(holder_id, "unknown_resolution_holder_id")
    if _RELEASE_SHA_RE.fullmatch(release_sha) is None:
        raise ExecutionInvariantError("unknown_resolution_release_sha_is_invalid")
    _require_positive_int(fencing_token, "unknown_resolution_fencing_token")


def _require_uuid(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise ExecutionInvariantError(f"{field}_is_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutionInvariantError(f"{field}_is_invalid") from exc
    if str(parsed) != value.lower():
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_nonnegative_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionInvariantError(f"{field}_must_be_nonnegative")


def _require_positive_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(f"{field}_must_be_positive")


def _require_aware(value: object, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError(f"{field}_must_be_timezone_aware")
