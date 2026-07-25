from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject, JsonValue

SCHEDULER_DEFINITION_SCHEMA_VERSION = "durable_scheduler_job_definition.v1"
SCHEDULER_JOB_KEYS: frozenset[str] = frozenset(
    {
        "operations.commands",
        "operations.execution",
        "operations.settlement",
        "operations.reconciliation",
        "operations.outbox",
    }
)

SchedulerJobKey = Literal[
    "operations.commands",
    "operations.execution",
    "operations.settlement",
    "operations.reconciliation",
    "operations.outbox",
]
SchedulerRunState = Literal[
    "pending",
    "leased",
    "retry_wait",
    "succeeded",
    "dead_letter",
]
SchedulerDefinitionState = Literal["ready", "blocked"]
SchedulerConvergenceStatus = Literal[
    "converged",
    "claimed",
    "wait",
    "manual_resolution",
]

SCHEDULER_EFFECTFUL_JOB_KEYS: frozenset[SchedulerJobKey] = frozenset(
    {"operations.execution", "operations.settlement"}
)
SCHEDULER_RETRYABLE_REASONS: Mapping[SchedulerJobKey, frozenset[str]] = MappingProxyType(
    {
        "operations.commands": frozenset({"command_poll_retryable"}),
        "operations.execution": frozenset(),
        "operations.settlement": frozenset(),
        "operations.reconciliation": frozenset({"reconciliation_poll_retryable"}),
        "operations.outbox": frozenset({"outbox_poll_retryable"}),
    }
)

_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_MAX_DATABASE_BIGINT = 9_223_372_036_854_775_807
_MIN_RESULT_INTEGER = -(2**63)
_MAX_RESULT_INTEGER = 2**63 - 1
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_NODES = 256
_MAX_RESULT_STRING_LENGTH = 4_096
_MAX_RESULT_SERIALIZED_BYTES = 32 * 1_024


class SchedulerInvariantError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("durable_scheduler", safe_message)


@dataclass(frozen=True, slots=True)
class ScheduledJobDefinitionV1:
    job_key: SchedulerJobKey
    interval_seconds: int
    lease_ttl_seconds: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    max_manual_replays: int
    enabled: bool = True
    schema_version: str = SCHEDULER_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_job_key(self.job_key)
        _require_bounded_positive_int(
            self.interval_seconds,
            "definition_interval_seconds",
            maximum=86_400,
        )
        _require_bounded_positive_int(
            self.lease_ttl_seconds,
            "definition_lease_ttl_seconds",
            maximum=3_600,
        )
        if self.lease_ttl_seconds < 10:
            raise SchedulerInvariantError("definition_lease_ttl_is_below_safe_minimum")
        _require_bounded_positive_int(
            self.max_attempts,
            "definition_max_attempts",
            maximum=100,
        )
        _require_bounded_positive_int(
            self.retry_base_seconds,
            "definition_retry_base_seconds",
            maximum=86_400,
        )
        _require_bounded_positive_int(
            self.retry_max_seconds,
            "definition_retry_max_seconds",
            maximum=604_800,
        )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise SchedulerInvariantError("definition_retry_window_is_invalid")
        _require_bounded_nonnegative_int(
            self.max_manual_replays,
            "definition_max_manual_replays",
            maximum=100,
        )
        if type(self.enabled) is not bool:
            raise SchedulerInvariantError("definition_enabled_is_invalid")
        if self.schema_version != SCHEDULER_DEFINITION_SCHEMA_VERSION:
            raise SchedulerInvariantError("definition_schema_version_is_invalid")

    @property
    def definition_sha256(self) -> str:
        return _payload_sha256(self.to_payload())

    def to_payload(self) -> JsonObject:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "job_key": self.job_key,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "max_attempts": self.max_attempts,
            "max_manual_replays": self.max_manual_replays,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ScheduledJobRunV1:
    run_id: str
    account_id: str
    job_key: SchedulerJobKey
    definition_sha256: str
    state: SchedulerRunState
    revision: int
    attempt_count: int
    replay_generation: int
    replay_of_run_id: str | None
    scheduled_for: datetime
    available_at: datetime
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.run_id, "run_id")
        _require_account_id(self.account_id)
        _require_job_key(self.job_key)
        _require_sha256(self.definition_sha256, "run_definition_sha256")
        if self.state not in {
            "pending",
            "leased",
            "retry_wait",
            "succeeded",
            "dead_letter",
        }:
            raise SchedulerInvariantError("run_state_is_invalid")
        _require_bounded_positive_int(
            self.revision,
            "run_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_bounded_nonnegative_int(
            self.attempt_count,
            "run_attempt_count",
            maximum=100,
        )
        if (
            self.state in {"leased", "retry_wait", "succeeded", "dead_letter"}
            and self.attempt_count == 0
        ):
            raise SchedulerInvariantError("run_state_requires_attempt")
        _require_bounded_nonnegative_int(
            self.replay_generation,
            "run_replay_generation",
            maximum=100,
        )
        if self.replay_generation == 0:
            if self.replay_of_run_id is not None:
                raise SchedulerInvariantError("original_run_has_replay_parent")
        else:
            if self.replay_of_run_id is None:
                raise SchedulerInvariantError("replayed_run_requires_parent")
            _require_uuid(self.replay_of_run_id, "run_replay_of_run_id")
            if self.replay_of_run_id == self.run_id:
                raise SchedulerInvariantError("run_cannot_replay_itself")
        scheduled_for = _utc(self.scheduled_for, "run_scheduled_for")
        available_at = _utc(self.available_at, "run_available_at")
        created_at = _utc(self.created_at, "run_created_at")
        updated_at = _utc(self.updated_at, "run_updated_at")
        if available_at < scheduled_for:
            raise SchedulerInvariantError("run_available_before_schedule")
        if updated_at < created_at:
            raise SchedulerInvariantError("run_update_precedes_creation")
        object.__setattr__(self, "scheduled_for", scheduled_for)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobLeaseV1:
    lease_token: str
    run_id: str
    account_id: str
    holder_id: str
    release_sha: str
    outer_fencing_token: int
    attempt_number: int
    run_revision: int
    leased_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.lease_token, "lease_token")
        _require_uuid(self.run_id, "lease_run_id")
        _require_account_id(self.account_id)
        _require_uuid(self.holder_id, "lease_holder_id")
        _require_release_sha(self.release_sha, "lease_release_sha")
        _require_bounded_positive_int(
            self.outer_fencing_token,
            "lease_outer_fencing_token",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_bounded_positive_int(
            self.attempt_number,
            "lease_attempt_number",
            maximum=100,
        )
        _require_bounded_positive_int(
            self.run_revision,
            "lease_run_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        leased_at = _utc(self.leased_at, "lease_leased_at")
        lease_expires_at = _utc(self.lease_expires_at, "lease_expires_at")
        if lease_expires_at <= leased_at:
            raise SchedulerInvariantError("lease_expiry_must_follow_acquisition")
        object.__setattr__(self, "leased_at", leased_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobClaimV1:
    definition: ScheduledJobDefinitionV1
    run: ScheduledJobRunV1
    lease: ScheduledJobLeaseV1
    observed_at: datetime

    def __post_init__(self) -> None:
        definition = canonical_scheduler_definition(self.definition)
        run = canonical_scheduler_run(self.run)
        lease = canonical_scheduler_lease(self.lease)
        observed_at = _utc(self.observed_at, "claim_observed_at")
        if run.state != "leased":
            raise SchedulerInvariantError("claim_run_is_not_leased")
        if definition.job_key != run.job_key:
            raise SchedulerInvariantError("claim_definition_job_key_mismatch")
        if definition.definition_sha256 != run.definition_sha256:
            raise SchedulerInvariantError("claim_definition_digest_mismatch")
        if lease.run_id != run.run_id or lease.account_id != run.account_id:
            raise SchedulerInvariantError("claim_lease_run_identity_mismatch")
        if lease.attempt_number != run.attempt_count:
            raise SchedulerInvariantError("claim_attempt_number_mismatch")
        if lease.run_revision != run.revision:
            raise SchedulerInvariantError("claim_run_revision_mismatch")
        if run.attempt_count > definition.max_attempts:
            raise SchedulerInvariantError("claim_attempt_budget_exceeded")
        if run.replay_generation > definition.max_manual_replays:
            raise SchedulerInvariantError("claim_replay_budget_exceeded")
        if lease.lease_expires_at - lease.leased_at > timedelta(
            seconds=definition.lease_ttl_seconds
        ):
            raise SchedulerInvariantError("claim_lease_ttl_exceeded")
        if run.available_at > observed_at:
            raise SchedulerInvariantError("claim_run_is_not_available")
        if not lease.leased_at <= observed_at < lease.lease_expires_at:
            raise SchedulerInvariantError("claim_database_clock_outside_lease")
        if lease.leased_at != observed_at:
            raise SchedulerInvariantError("claim_lease_time_does_not_match_observation")
        if run.updated_at != observed_at:
            raise SchedulerInvariantError("claim_run_update_does_not_match_observation")
        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "run", run)
        object.__setattr__(self, "lease", lease)
        object.__setattr__(self, "observed_at", observed_at)

    @property
    def job_key(self) -> SchedulerJobKey:
        return self.run.job_key


@dataclass(frozen=True, slots=True)
class ScheduledJobClaimReceiptV1:
    claim: ScheduledJobClaimV1 | None
    observed_at: datetime

    def __post_init__(self) -> None:
        observed_at = _utc(self.observed_at, "claim_receipt_observed_at")
        claim = None if self.claim is None else canonical_scheduler_claim(self.claim)
        if claim is not None and claim.observed_at != observed_at:
            raise SchedulerInvariantError("claim_receipt_observation_mismatch")
        object.__setattr__(self, "claim", claim)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobDefinitionReceiptV1:
    definition_id: str
    account_id: str
    job_key: SchedulerJobKey
    definition_sha256: str
    revision: int
    next_due_at: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.definition_id, "definition_receipt_definition_id")
        _require_account_id(self.account_id)
        _require_job_key(self.job_key)
        _require_sha256(self.definition_sha256, "definition_receipt_sha256")
        _require_bounded_positive_int(
            self.revision,
            "definition_receipt_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        next_due_at = _utc(self.next_due_at, "definition_receipt_next_due_at")
        observed_at = _utc(self.observed_at, "definition_receipt_observed_at")
        object.__setattr__(self, "next_due_at", next_due_at)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobConvergenceDefinitionV1:
    definition_id: str
    account_id: str
    definition: ScheduledJobDefinitionV1
    revision: int
    next_due_at: datetime
    scheduler_state: SchedulerDefinitionState

    def __post_init__(self) -> None:
        _require_uuid(self.definition_id, "convergence_definition_id")
        _require_account_id(self.account_id)
        definition = canonical_scheduler_definition(self.definition)
        _require_bounded_positive_int(
            self.revision,
            "convergence_definition_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        next_due_at = _utc(
            self.next_due_at,
            "convergence_definition_next_due_at",
        )
        if self.scheduler_state not in {"ready", "blocked"}:
            raise SchedulerInvariantError("convergence_definition_state_is_invalid")
        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "next_due_at", next_due_at)

    @property
    def job_key(self) -> SchedulerJobKey:
        return self.definition.job_key

    @property
    def definition_sha256(self) -> str:
        return self.definition.definition_sha256


@dataclass(frozen=True, slots=True)
class SchedulerDefinitionConvergenceReceiptV1:
    status: SchedulerConvergenceStatus
    definition: ScheduledJobConvergenceDefinitionV1
    claim: ScheduledJobClaimV1 | None
    active_run_id: str | None
    next_eligible_at: datetime | None
    reason_code: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if type(self.definition) is not ScheduledJobConvergenceDefinitionV1:
            raise SchedulerInvariantError("convergence_definition_is_invalid")
        definition = ScheduledJobConvergenceDefinitionV1(
            definition_id=self.definition.definition_id,
            account_id=self.definition.account_id,
            definition=self.definition.definition,
            revision=self.definition.revision,
            next_due_at=self.definition.next_due_at,
            scheduler_state=self.definition.scheduler_state,
        )
        observed_at = _utc(self.observed_at, "convergence_observed_at")
        claim = None if self.claim is None else canonical_scheduler_claim(self.claim)
        active_run_id = self.active_run_id
        if active_run_id is not None:
            _require_uuid(active_run_id, "convergence_active_run_id")
        next_eligible_at = _optional_utc(
            self.next_eligible_at,
            "convergence_next_eligible_at",
        )
        reason_code = self.reason_code
        if reason_code is not None:
            _require_reason(reason_code, "convergence_reason_code")

        if self.status == "converged":
            if (
                claim is not None
                or active_run_id is not None
                or next_eligible_at is not None
                or reason_code is not None
                or definition.scheduler_state != "ready"
            ):
                raise SchedulerInvariantError("converged_receipt_is_not_quiescent")
        elif self.status == "claimed":
            if (
                claim is None
                or active_run_id != claim.run.run_id
                or next_eligible_at is not None
                or reason_code is not None
                or definition.scheduler_state != "ready"
                or definition.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
                or claim.observed_at != observed_at
                or definition.account_id != claim.run.account_id
                or definition.definition != claim.definition
            ):
                raise SchedulerInvariantError("claimed_convergence_receipt_is_invalid")
        elif self.status == "wait":
            if (
                claim is not None
                or active_run_id is None
                or next_eligible_at is None
                or next_eligible_at <= observed_at
                or reason_code is None
            ):
                raise SchedulerInvariantError("waiting_convergence_receipt_is_invalid")
        elif self.status == "manual_resolution":
            if (
                claim is not None
                or active_run_id is None
                or next_eligible_at is not None
                or reason_code is None
            ):
                raise SchedulerInvariantError("manual_convergence_receipt_is_invalid")
        else:
            raise SchedulerInvariantError("convergence_status_is_invalid")

        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "claim", claim)
        object.__setattr__(self, "next_eligible_at", next_eligible_at)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobCompletionReceiptV1:
    run_id: str
    run_revision: int
    attempt_count: int
    next_attempt_at: None
    failure_reason_code: None
    result_sha256: str
    observed_at: datetime
    state: Literal["succeeded"] = "succeeded"

    def __post_init__(self) -> None:
        _require_uuid(self.run_id, "completion_run_id")
        _require_bounded_positive_int(
            self.run_revision,
            "completion_run_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_bounded_positive_int(
            self.attempt_count,
            "completion_attempt_count",
            maximum=100,
        )
        if self.next_attempt_at is not None or self.failure_reason_code is not None:
            raise SchedulerInvariantError("completion_retry_fields_are_invalid")
        _require_sha256(self.result_sha256, "completion_result_sha256")
        observed_at = _utc(self.observed_at, "completion_observed_at")
        if self.state != "succeeded":
            raise SchedulerInvariantError("completion_receipt_is_invalid")
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class ScheduledJobFailureReceiptV1:
    run_id: str
    run_revision: int
    attempt_count: int
    state: Literal["retry_wait", "dead_letter"]
    failure_reason_code: str
    result_sha256: str
    next_attempt_at: datetime | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.run_id, "failure_run_id")
        _require_bounded_positive_int(
            self.run_revision,
            "failure_run_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_bounded_positive_int(
            self.attempt_count,
            "failure_attempt_count",
            maximum=100,
        )
        _require_reason(self.failure_reason_code, "failure_reason_code")
        _require_sha256(self.result_sha256, "failure_result_sha256")
        observed_at = _utc(self.observed_at, "failure_observed_at")
        next_attempt_at = _optional_utc(
            self.next_attempt_at,
            "failure_next_attempt_at",
        )
        if self.state == "retry_wait":
            # An idempotent recovery response is authorized at the retry RPC's
            # current DB time, while next_attempt_at remains bound to the
            # original transition. It may therefore already be due.
            if next_attempt_at is None:
                raise SchedulerInvariantError("retry_wait_receipt_is_invalid")
        elif self.state == "dead_letter":
            if next_attempt_at is not None:
                raise SchedulerInvariantError("dead_letter_receipt_is_invalid")
        else:
            raise SchedulerInvariantError("failure_receipt_state_is_invalid")
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class SchedulerDeadLetterV1:
    source_run_id: str
    account_id: str
    job_key: SchedulerJobKey
    definition_sha256: str
    source_revision: int
    attempt_count: int
    failure_reason_code: str
    failure_sha256: str
    replay_generation: int
    max_manual_replays: int
    dead_lettered_at: datetime
    observed_at: datetime
    state: Literal["dead_letter"] = "dead_letter"

    def __post_init__(self) -> None:
        _require_uuid(self.source_run_id, "dead_letter_source_run_id")
        _require_account_id(self.account_id)
        _require_job_key(self.job_key)
        _require_sha256(self.definition_sha256, "dead_letter_definition_sha256")
        _require_bounded_positive_int(
            self.source_revision,
            "dead_letter_source_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_bounded_positive_int(
            self.attempt_count,
            "dead_letter_attempt_count",
            maximum=100,
        )
        _require_reason(self.failure_reason_code, "dead_letter_failure_reason")
        _require_sha256(self.failure_sha256, "dead_letter_failure_sha256")
        _require_bounded_nonnegative_int(
            self.replay_generation,
            "dead_letter_replay_generation",
            maximum=100,
        )
        _require_bounded_nonnegative_int(
            self.max_manual_replays,
            "dead_letter_max_manual_replays",
            maximum=100,
        )
        dead_lettered_at = _utc(self.dead_lettered_at, "dead_lettered_at")
        observed_at = _utc(self.observed_at, "dead_letter_observed_at")
        if dead_lettered_at > observed_at:
            raise SchedulerInvariantError("dead_letter_time_exceeds_observation")
        if self.state != "dead_letter":
            raise SchedulerInvariantError("dead_letter_state_is_invalid")
        object.__setattr__(self, "dead_lettered_at", dead_lettered_at)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class SchedulerReplayAssessmentV1:
    dead_letter: SchedulerDeadLetterV1
    eligible: bool
    ineligibility_reason: str | None

    def __post_init__(self) -> None:
        dead_letter = canonical_scheduler_dead_letter(self.dead_letter)
        if type(self.eligible) is not bool:
            raise SchedulerInvariantError("replay_assessment_eligible_is_invalid")
        if self.eligible:
            if self.ineligibility_reason is not None:
                raise SchedulerInvariantError("eligible_replay_has_ineligibility_reason")
            if dead_letter.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS:
                raise SchedulerInvariantError("effectful_scheduler_replay_cannot_be_eligible")
            if dead_letter.replay_generation >= dead_letter.max_manual_replays:
                raise SchedulerInvariantError("eligible_replay_exceeds_budget")
        else:
            if self.ineligibility_reason is None:
                raise SchedulerInvariantError("ineligible_replay_requires_reason")
            _require_reason(
                self.ineligibility_reason,
                "replay_assessment_ineligibility_reason",
            )
        object.__setattr__(self, "dead_letter", dead_letter)


@dataclass(frozen=True, slots=True)
class SchedulerDeadLetterInspectionReceiptV1:
    assessment: SchedulerReplayAssessmentV1 | None
    observed_at: datetime

    def __post_init__(self) -> None:
        observed_at = _utc(self.observed_at, "inspection_receipt_observed_at")
        assessment = (
            None
            if self.assessment is None
            else canonical_scheduler_replay_assessment(self.assessment)
        )
        if (
            assessment is not None
            and assessment.dead_letter.observed_at != observed_at
        ):
            raise SchedulerInvariantError("inspection_receipt_observation_mismatch")
        object.__setattr__(self, "assessment", assessment)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class SchedulerReplayReceiptV1:
    source_run_id: str
    new_run_id: str
    replay_request_id: str
    job_key: SchedulerJobKey
    definition_sha256: str
    source_revision: int
    failure_reason_code: str
    replay_generation: int
    created_at: datetime
    observed_at: datetime
    idempotent: bool
    state: Literal["pending"] = "pending"

    def __post_init__(self) -> None:
        _require_uuid(self.source_run_id, "replay_source_run_id")
        _require_uuid(self.new_run_id, "replay_new_run_id")
        _require_uuid(self.replay_request_id, "replay_request_id")
        if self.source_run_id == self.new_run_id:
            raise SchedulerInvariantError("replay_must_create_new_run")
        _require_job_key(self.job_key)
        _require_sha256(self.definition_sha256, "replay_definition_sha256")
        _require_bounded_positive_int(
            self.source_revision,
            "replay_source_revision",
            maximum=_MAX_DATABASE_BIGINT,
        )
        _require_reason(self.failure_reason_code, "replay_failure_reason_code")
        _require_bounded_positive_int(
            self.replay_generation,
            "replay_generation",
            maximum=100,
        )
        created_at = _utc(self.created_at, "replay_created_at")
        observed_at = _utc(self.observed_at, "replay_observed_at")
        if created_at > observed_at:
            raise SchedulerInvariantError("replay_creation_exceeds_observation")
        if type(self.idempotent) is not bool or self.state != "pending":
            raise SchedulerInvariantError("replay_receipt_is_invalid")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "observed_at", observed_at)


def scheduler_result_sha256(value: object) -> str:
    """Hash a deterministic JSON-compatible handler result without persisting it."""

    nodes = [0]
    canonical = _canonical_result_value(
        value,
        depth=0,
        nodes=nodes,
        active_containers=set(),
    )
    payload: JsonObject = {
        "schema_version": "durable_scheduler_handler_result.v1",
        "value": canonical,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise SchedulerInvariantError("handler_result_is_not_canonical_json") from None
    if len(encoded) > _MAX_RESULT_SERIALIZED_BYTES:
        raise SchedulerInvariantError("handler_result_serialized_size_exceeded")
    return hashlib.sha256(encoded).hexdigest()


def scheduler_retry_delay(
    definition: ScheduledJobDefinitionV1,
    attempt_count: int,
) -> timedelta:
    canonical = canonical_scheduler_definition(definition)
    _require_bounded_positive_int(
        attempt_count,
        "retry_delay_attempt_count",
        maximum=100,
    )
    return timedelta(
        seconds=min(
            canonical.retry_max_seconds,
            canonical.retry_base_seconds * (2 ** min(attempt_count - 1, 30)),
        )
    )


def scheduler_definition_budget_is_safe(
    definition: ScheduledJobDefinitionV1,
) -> bool:
    canonical = canonical_scheduler_definition(definition)
    if canonical.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS:
        return canonical.max_attempts == 1 and canonical.max_manual_replays == 0
    return canonical.max_attempts <= 3 and canonical.max_manual_replays <= 1


def scheduler_replay_budget_is_safe(dead_letter: SchedulerDeadLetterV1) -> bool:
    canonical = canonical_scheduler_dead_letter(dead_letter)
    if canonical.job_key in SCHEDULER_EFFECTFUL_JOB_KEYS:
        return canonical.max_manual_replays == 0
    return canonical.max_manual_replays <= 1


def canonical_scheduler_definition(value: object) -> ScheduledJobDefinitionV1:
    if type(value) is not ScheduledJobDefinitionV1:
        raise SchedulerInvariantError("definition_is_invalid")
    try:
        canonical = ScheduledJobDefinitionV1(
            job_key=value.job_key,
            interval_seconds=value.interval_seconds,
            lease_ttl_seconds=value.lease_ttl_seconds,
            max_attempts=value.max_attempts,
            retry_base_seconds=value.retry_base_seconds,
            retry_max_seconds=value.retry_max_seconds,
            max_manual_replays=value.max_manual_replays,
            enabled=value.enabled,
            schema_version=value.schema_version,
        )
    except Exception:
        raise SchedulerInvariantError("definition_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("definition_is_invalid")
    return canonical


def canonical_scheduler_datetime(value: object, field_name: str) -> datetime:
    """Return an exact built-in UTC datetime for scheduler trust boundaries."""

    return _utc(value, field_name)


def canonical_scheduler_run(value: object) -> ScheduledJobRunV1:
    if type(value) is not ScheduledJobRunV1:
        raise SchedulerInvariantError("run_is_invalid")
    try:
        canonical = ScheduledJobRunV1(
            run_id=value.run_id,
            account_id=value.account_id,
            job_key=value.job_key,
            definition_sha256=value.definition_sha256,
            state=value.state,
            revision=value.revision,
            attempt_count=value.attempt_count,
            replay_generation=value.replay_generation,
            replay_of_run_id=value.replay_of_run_id,
            scheduled_for=value.scheduled_for,
            available_at=value.available_at,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )
    except Exception:
        raise SchedulerInvariantError("run_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("run_is_invalid")
    return canonical


def canonical_scheduler_lease(value: object) -> ScheduledJobLeaseV1:
    if type(value) is not ScheduledJobLeaseV1:
        raise SchedulerInvariantError("lease_is_invalid")
    try:
        canonical = ScheduledJobLeaseV1(
            lease_token=value.lease_token,
            run_id=value.run_id,
            account_id=value.account_id,
            holder_id=value.holder_id,
            release_sha=value.release_sha,
            outer_fencing_token=value.outer_fencing_token,
            attempt_number=value.attempt_number,
            run_revision=value.run_revision,
            leased_at=value.leased_at,
            lease_expires_at=value.lease_expires_at,
        )
    except Exception:
        raise SchedulerInvariantError("lease_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("lease_is_invalid")
    return canonical


def canonical_scheduler_claim(value: object) -> ScheduledJobClaimV1:
    if type(value) is not ScheduledJobClaimV1:
        raise SchedulerInvariantError("claim_is_invalid")
    try:
        canonical = ScheduledJobClaimV1(
            definition=value.definition,
            run=value.run,
            lease=value.lease,
            observed_at=value.observed_at,
        )
    except Exception:
        raise SchedulerInvariantError("claim_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("claim_is_invalid")
    return canonical


def canonical_scheduler_claim_receipt(value: object) -> ScheduledJobClaimReceiptV1:
    if type(value) is not ScheduledJobClaimReceiptV1:
        raise SchedulerInvariantError("claim_receipt_is_invalid")
    try:
        canonical = ScheduledJobClaimReceiptV1(
            claim=value.claim,
            observed_at=value.observed_at,
        )
    except Exception:
        raise SchedulerInvariantError("claim_receipt_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("claim_receipt_is_invalid")
    return canonical


def canonical_scheduler_convergence_receipt(
    value: object,
) -> SchedulerDefinitionConvergenceReceiptV1:
    if type(value) is not SchedulerDefinitionConvergenceReceiptV1:
        raise SchedulerInvariantError("convergence_receipt_is_invalid")
    try:
        canonical = SchedulerDefinitionConvergenceReceiptV1(
            status=value.status,
            definition=value.definition,
            claim=value.claim,
            active_run_id=value.active_run_id,
            next_eligible_at=value.next_eligible_at,
            reason_code=value.reason_code,
            observed_at=value.observed_at,
        )
    except Exception:
        raise SchedulerInvariantError("convergence_receipt_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("convergence_receipt_is_invalid")
    return canonical


def canonical_scheduler_dead_letter(value: object) -> SchedulerDeadLetterV1:
    if type(value) is not SchedulerDeadLetterV1:
        raise SchedulerInvariantError("dead_letter_is_invalid")
    try:
        canonical = SchedulerDeadLetterV1(
            source_run_id=value.source_run_id,
            account_id=value.account_id,
            job_key=value.job_key,
            definition_sha256=value.definition_sha256,
            source_revision=value.source_revision,
            attempt_count=value.attempt_count,
            failure_reason_code=value.failure_reason_code,
            failure_sha256=value.failure_sha256,
            replay_generation=value.replay_generation,
            max_manual_replays=value.max_manual_replays,
            dead_lettered_at=value.dead_lettered_at,
            observed_at=value.observed_at,
            state=value.state,
        )
    except Exception:
        raise SchedulerInvariantError("dead_letter_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("dead_letter_is_invalid")
    return canonical


def canonical_scheduler_replay_assessment(value: object) -> SchedulerReplayAssessmentV1:
    if type(value) is not SchedulerReplayAssessmentV1:
        raise SchedulerInvariantError("replay_assessment_is_invalid")
    try:
        canonical = SchedulerReplayAssessmentV1(
            dead_letter=value.dead_letter,
            eligible=value.eligible,
            ineligibility_reason=value.ineligibility_reason,
        )
    except Exception:
        raise SchedulerInvariantError("replay_assessment_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("replay_assessment_is_invalid")
    return canonical


def canonical_scheduler_inspection_receipt(
    value: object,
) -> SchedulerDeadLetterInspectionReceiptV1:
    if type(value) is not SchedulerDeadLetterInspectionReceiptV1:
        raise SchedulerInvariantError("inspection_receipt_is_invalid")
    try:
        canonical = SchedulerDeadLetterInspectionReceiptV1(
            assessment=value.assessment,
            observed_at=value.observed_at,
        )
    except Exception:
        raise SchedulerInvariantError("inspection_receipt_is_invalid") from None
    if canonical != value:
        raise SchedulerInvariantError("inspection_receipt_is_invalid")
    return canonical


def _canonical_result_value(
    value: object,
    *,
    depth: int,
    nodes: list[int],
    active_containers: set[int],
) -> JsonValue:
    if depth > _MAX_RESULT_DEPTH:
        raise SchedulerInvariantError("handler_result_depth_exceeded")
    nodes[0] += 1
    if nodes[0] > _MAX_RESULT_NODES:
        raise SchedulerInvariantError("handler_result_node_count_exceeded")
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        if len(value) > _MAX_RESULT_STRING_LENGTH:
            raise SchedulerInvariantError("handler_result_string_length_exceeded")
        return value
    if type(value) is int:
        integer = value
        if not _MIN_RESULT_INTEGER <= integer <= _MAX_RESULT_INTEGER:
            raise SchedulerInvariantError("handler_result_integer_is_out_of_range")
        return integer
    if isinstance(value, Enum):
        return _canonical_result_value(
            value.value,
            depth=depth + 1,
            nodes=nodes,
            active_containers=active_containers,
        )
    if is_dataclass(value) and not isinstance(value, type):
        with _result_container(value, active_containers):
            dataclass_result: JsonObject = {}
            for field_info in fields(value):
                if not field_info.name or len(field_info.name) > _MAX_RESULT_STRING_LENGTH:
                    raise SchedulerInvariantError("handler_result_mapping_key_is_invalid")
                dataclass_result[field_info.name] = _canonical_result_value(
                    getattr(value, field_info.name),
                    depth=depth + 1,
                    nodes=nodes,
                    active_containers=active_containers,
                )
            return dataclass_result
    if type(value) in {tuple, list}:
        with _result_container(value, active_containers):
            return [
                _canonical_result_value(
                    item,
                    depth=depth + 1,
                    nodes=nodes,
                    active_containers=active_containers,
                )
                for item in cast(list[object] | tuple[object, ...], value)
            ]
    if type(value) is dict:
        with _result_container(value, active_containers):
            mapping_result: JsonObject = {}
            for key, entry_value in cast(dict[object, object], value).items():
                if (
                    type(key) is not str
                    or not key
                    or len(key) > _MAX_RESULT_STRING_LENGTH
                    or key in mapping_result
                ):
                    raise SchedulerInvariantError("handler_result_mapping_key_is_invalid")
                mapping_result[key] = _canonical_result_value(
                    entry_value,
                    depth=depth + 1,
                    nodes=nodes,
                    active_containers=active_containers,
                )
            return mapping_result
    raise SchedulerInvariantError("handler_result_is_not_canonical_json")


class _result_container:
    def __init__(self, value: object, active_containers: set[int]) -> None:
        self.identity = id(value)
        self.active_containers = active_containers

    def __enter__(self) -> None:
        if self.identity in self.active_containers:
            raise SchedulerInvariantError("handler_result_cycle_detected")
        self.active_containers.add(self.identity)

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.active_containers.remove(self.identity)


def _payload_sha256(payload: JsonObject) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_job_key(value: object) -> None:
    if type(value) is not str or value not in SCHEDULER_JOB_KEYS:
        raise SchedulerInvariantError("job_key_is_not_allowed")


def _require_account_id(value: object) -> None:
    if type(value) is not str or _ACCOUNT_RE.fullmatch(value) is None:
        raise SchedulerInvariantError("account_id_is_invalid")


def _require_uuid(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise SchedulerInvariantError(f"{field_name}_must_be_canonical_uuid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise SchedulerInvariantError(f"{field_name}_must_be_canonical_uuid") from None
    if str(parsed) != value:
        raise SchedulerInvariantError(f"{field_name}_must_be_canonical_uuid")


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_must_be_sha256")


def _require_release_sha(value: object, field_name: str) -> None:
    if type(value) is not str or _RELEASE_SHA_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_must_be_release_sha")


def _require_reason(value: object, field_name: str) -> None:
    if type(value) is not str or _REASON_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _require_bounded_positive_int(value: object, field_name: str, *, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _require_bounded_nonnegative_int(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise SchedulerInvariantError(f"{field_name}_must_be_timezone_aware")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        normalized = value.astimezone(UTC)
    except Exception:
        raise SchedulerInvariantError(
            f"{field_name}_must_be_timezone_aware"
        ) from None
    if type(normalized) is not datetime or normalized.tzinfo is not UTC:
        raise SchedulerInvariantError(f"{field_name}_must_be_timezone_aware")
    return normalized


def _optional_utc(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _utc(value, field_name)
