from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobConvergenceDefinitionV1,
    ScheduledJobDefinitionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDeadLetterInspectionReceiptV1,
    SchedulerDeadLetterV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
    SchedulerReplayAssessmentV1,
    SchedulerReplayReceiptV1,
)

_DEFINITION_FIELDS = frozenset(
    {
        "schema_version",
        "job_key",
        "interval_seconds",
        "lease_ttl_seconds",
        "max_attempts",
        "retry_base_seconds",
        "retry_max_seconds",
        "max_manual_replays",
        "enabled",
        "definition_sha256",
    }
)
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "account_id",
        "job_key",
        "definition_sha256",
        "state",
        "revision",
        "attempt_count",
        "replay_generation",
        "replay_of_run_id",
        "scheduled_for",
        "available_at",
        "created_at",
        "updated_at",
    }
)
_LEASE_FIELDS = frozenset(
    {
        "lease_token",
        "run_id",
        "account_id",
        "holder_id",
        "release_sha",
        "outer_fencing_token",
        "attempt_number",
        "run_revision",
        "leased_at",
        "lease_expires_at",
    }
)
_DEAD_LETTER_FIELDS = frozenset(
    {
        "source_run_id",
        "account_id",
        "job_key",
        "definition_sha256",
        "source_revision",
        "attempt_count",
        "failure_reason_code",
        "failure_sha256",
        "replay_generation",
        "max_manual_replays",
        "dead_lettered_at",
        "state",
    }
)
_ENSURE_RECEIPT_FIELDS = frozenset(
    {
        "definition_id",
        "account_id",
        "job_key",
        "definition_sha256",
        "revision",
        "next_due_at",
        "observed_at",
    }
)
_CLAIM_RESPONSE_FIELDS = frozenset({"claimed", "claim", "observed_at"})
_CLAIM_FIELDS = frozenset({"definition", "run", "lease"})
_SETTLEMENT_FIELDS = frozenset(
    {
        "run_id",
        "state",
        "run_revision",
        "attempt_count",
        "next_attempt_at",
        "failure_reason_code",
        "result_sha256",
        "observed_at",
    }
)
_INSPECT_FIELDS = frozenset(
    {
        "found",
        "dead_letter",
        "eligible",
        "ineligibility_reason",
        "observed_at",
    }
)
_REPLAY_RECEIPT_FIELDS = frozenset(
    {
        "source_run_id",
        "new_run_id",
        "replay_request_id",
        "job_key",
        "definition_sha256",
        "source_revision",
        "failure_reason_code",
        "replay_generation",
        "state",
        "created_at",
        "observed_at",
        "idempotent",
    }
)
_CONVERGENCE_FIELDS = frozenset(
    {
        "status",
        "definition",
        "claim",
        "active_run_id",
        "next_eligible_at",
        "reason_code",
        "observed_at",
    }
)
_CONVERGENCE_DEFINITION_FIELDS = _DEFINITION_FIELDS | frozenset(
    {
        "definition_id",
        "account_id",
        "revision",
        "next_due_at",
        "scheduler_state",
    }
)


class DurableSchedulerWireCodec:
    """Encode fixed RPC arguments and decode exact untrusted response shapes."""

    def ensure_payload(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
        release_sha: str,
    ) -> JsonObject:
        return {
            **self.actor_payload(outer_lease, release_sha),
            "p_job_key": definition.job_key,
            "p_definition_sha256": definition.definition_sha256,
            "p_interval_seconds": definition.interval_seconds,
            "p_lease_ttl_seconds": definition.lease_ttl_seconds,
            "p_max_attempts": definition.max_attempts,
            "p_retry_base_seconds": definition.retry_base_seconds,
            "p_retry_max_seconds": definition.retry_max_seconds,
            "p_max_manual_replays": definition.max_manual_replays,
            "p_enabled": definition.enabled,
        }

    def actor_payload(self, lease: WorkerLease, release_sha: str) -> JsonObject:
        return {
            "p_account_id": lease.account_id,
            "p_holder_id": lease.holder_id,
            "p_outer_fencing_token": lease.fencing_token,
            "p_release_sha": release_sha,
        }

    def convergence_payload(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
        release_sha: str,
    ) -> JsonObject:
        return self.ensure_payload(
            definition,
            outer_lease=outer_lease,
            release_sha=release_sha,
        )

    def complete_payload(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        release_sha: str,
        result_sha256: str,
    ) -> JsonObject:
        return {
            **self.actor_payload(outer_lease, release_sha),
            **self._claim_transition_payload(claim),
            "p_result_sha256": result_sha256,
        }

    def fail_payload(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        release_sha: str,
        failure_reason_code: str,
        failure_sha256: str,
        retryable: bool,
    ) -> JsonObject:
        return {
            **self.actor_payload(outer_lease, release_sha),
            **self._claim_transition_payload(claim),
            "p_failure_reason_code": failure_reason_code,
            "p_failure_sha256": failure_sha256,
            "p_retryable": retryable,
        }

    def inspect_payload(
        self,
        source_run_id: str,
        *,
        outer_lease: WorkerLease,
        release_sha: str,
    ) -> JsonObject:
        return {
            **self.actor_payload(outer_lease, release_sha),
            "p_source_run_id": source_run_id,
        }

    def replay_payload(
        self,
        assessment: SchedulerReplayAssessmentV1,
        *,
        replay_request_id: str,
        confirmed_reason_code: str,
        outer_lease: WorkerLease,
        release_sha: str,
    ) -> JsonObject:
        dead_letter = assessment.dead_letter
        return {
            **self.actor_payload(outer_lease, release_sha),
            "p_source_run_id": dead_letter.source_run_id,
            "p_expected_source_revision": dead_letter.source_revision,
            "p_expected_definition_sha256": dead_letter.definition_sha256,
            "p_expected_failure_reason_code": dead_letter.failure_reason_code,
            "p_expected_failure_sha256": dead_letter.failure_sha256,
            "p_expected_replay_generation": dead_letter.replay_generation,
            "p_replay_request_id": replay_request_id,
            "p_confirmed_reason_code": confirmed_reason_code,
            "p_explicit_confirmation": True,
        }

    def decode_definition_receipt(
        self,
        value: object,
    ) -> ScheduledJobDefinitionReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _ENSURE_RECEIPT_FIELDS)
        return ScheduledJobDefinitionReceiptV1(
            definition_id=_required_uuid(row, "definition_id"),
            account_id=_required_account_id(row, "account_id"),
            job_key=_required_job_key(row, "job_key"),
            definition_sha256=_required_sha256(row, "definition_sha256"),
            revision=_required_positive_int(row, "revision"),
            next_due_at=_required_datetime(row, "next_due_at"),
            observed_at=_required_datetime(row, "observed_at"),
        )

    def decode_claim(
        self,
        value: object,
    ) -> ScheduledJobClaimReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _CLAIM_RESPONSE_FIELDS)
        claimed = _required_bool(row, "claimed")
        observed_at = _required_datetime(row, "observed_at")
        raw_claim = row.get("claim")
        if not claimed:
            if raw_claim is not None:
                raise SchedulerInvariantError("scheduler_idle_claim_payload_is_invalid")
            return ScheduledJobClaimReceiptV1(
                claim=None,
                observed_at=observed_at,
            )
        claim_row = _required_object(raw_claim)
        _require_exact_keys(claim_row, _CLAIM_FIELDS)
        return ScheduledJobClaimReceiptV1(
            claim=ScheduledJobClaimV1(
                definition=_definition_from_row(
                    _required_object(claim_row.get("definition"))
                ),
                run=_run_from_row(_required_object(claim_row.get("run"))),
                lease=_lease_from_row(_required_object(claim_row.get("lease"))),
                observed_at=observed_at,
            ),
            observed_at=observed_at,
        )

    def decode_convergence(
        self,
        value: object,
    ) -> SchedulerDefinitionConvergenceReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _CONVERGENCE_FIELDS)
        raw_status = _required_text(row, "status")
        if raw_status not in {"converged", "claimed", "wait", "manual_resolution"}:
            raise SchedulerInvariantError("scheduler_convergence_status_is_invalid")
        observed_at = _required_datetime(row, "observed_at")
        raw_claim = row.get("claim")
        claim: ScheduledJobClaimV1 | None = None
        if raw_claim is not None:
            claim_row = _required_object(raw_claim)
            _require_exact_keys(claim_row, _CLAIM_FIELDS)
            claim = ScheduledJobClaimV1(
                definition=_definition_from_row(
                    _required_object(claim_row.get("definition"))
                ),
                run=_run_from_row(_required_object(claim_row.get("run"))),
                lease=_lease_from_row(_required_object(claim_row.get("lease"))),
                observed_at=observed_at,
            )
        return SchedulerDefinitionConvergenceReceiptV1(
            status=cast(
                Literal["converged", "claimed", "wait", "manual_resolution"],
                raw_status,
            ),
            definition=_convergence_definition_from_row(
                _required_object(row.get("definition"))
            ),
            claim=claim,
            active_run_id=_optional_uuid(row, "active_run_id"),
            next_eligible_at=_optional_datetime(row, "next_eligible_at"),
            reason_code=_optional_reason(row, "reason_code"),
            observed_at=observed_at,
        )

    def decode_completion(self, value: object) -> ScheduledJobCompletionReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _SETTLEMENT_FIELDS)
        if _required_text(row, "state") != "succeeded":
            raise SchedulerInvariantError("scheduler_completion_state_is_invalid")
        _required_none(row, "next_attempt_at")
        _required_none(row, "failure_reason_code")
        return ScheduledJobCompletionReceiptV1(
            run_id=_required_uuid(row, "run_id"),
            run_revision=_required_positive_int(row, "run_revision"),
            attempt_count=_required_positive_int(row, "attempt_count"),
            next_attempt_at=None,
            failure_reason_code=None,
            result_sha256=_required_sha256(row, "result_sha256"),
            observed_at=_required_datetime(row, "observed_at"),
            state="succeeded",
        )

    def decode_failure(self, value: object) -> ScheduledJobFailureReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _SETTLEMENT_FIELDS)
        state = _required_text(row, "state")
        if state not in {"retry_wait", "dead_letter"}:
            raise SchedulerInvariantError("scheduler_failure_state_is_invalid")
        return ScheduledJobFailureReceiptV1(
            run_id=_required_uuid(row, "run_id"),
            run_revision=_required_positive_int(row, "run_revision"),
            attempt_count=_required_positive_int(row, "attempt_count"),
            state=cast(Literal["retry_wait", "dead_letter"], state),
            failure_reason_code=_required_reason(row, "failure_reason_code"),
            result_sha256=_required_sha256(row, "result_sha256"),
            next_attempt_at=_optional_datetime(row, "next_attempt_at"),
            observed_at=_required_datetime(row, "observed_at"),
        )

    def decode_inspection(
        self,
        value: object,
    ) -> SchedulerDeadLetterInspectionReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _INSPECT_FIELDS)
        found = _required_bool(row, "found")
        observed_at = _required_datetime(row, "observed_at")
        if not found:
            if any(
                row.get(key) is not None
                for key in ("dead_letter", "eligible", "ineligibility_reason")
            ):
                raise SchedulerInvariantError("scheduler_missing_dead_letter_payload_invalid")
            return SchedulerDeadLetterInspectionReceiptV1(
                assessment=None,
                observed_at=observed_at,
            )
        return SchedulerDeadLetterInspectionReceiptV1(
            assessment=SchedulerReplayAssessmentV1(
                dead_letter=_dead_letter_from_row(
                    _required_object(row.get("dead_letter")),
                    observed_at=observed_at,
                ),
                eligible=_required_bool(row, "eligible"),
                ineligibility_reason=_optional_reason(row, "ineligibility_reason"),
            ),
            observed_at=observed_at,
        )

    def decode_replay(self, value: object) -> SchedulerReplayReceiptV1:
        row = _singleton_row(value)
        _require_exact_keys(row, _REPLAY_RECEIPT_FIELDS)
        if _required_text(row, "state") != "pending":
            raise SchedulerInvariantError("scheduler_replay_state_is_invalid")
        return SchedulerReplayReceiptV1(
            source_run_id=_required_uuid(row, "source_run_id"),
            new_run_id=_required_uuid(row, "new_run_id"),
            replay_request_id=_required_uuid(row, "replay_request_id"),
            job_key=_required_job_key(row, "job_key"),
            definition_sha256=_required_sha256(row, "definition_sha256"),
            source_revision=_required_positive_int(row, "source_revision"),
            failure_reason_code=_required_reason(row, "failure_reason_code"),
            replay_generation=_required_positive_int(row, "replay_generation"),
            created_at=_required_datetime(row, "created_at"),
            observed_at=_required_datetime(row, "observed_at"),
            idempotent=_required_bool(row, "idempotent"),
            state="pending",
        )

    def _claim_transition_payload(self, claim: ScheduledJobClaimV1) -> JsonObject:
        return {
            "p_run_id": claim.run.run_id,
            "p_expected_run_revision": claim.run.revision,
            "p_definition_sha256": claim.run.definition_sha256,
            "p_lease_token": claim.lease.lease_token,
        }


def _definition_from_row(row: dict[str, object]) -> ScheduledJobDefinitionV1:
    _require_exact_keys(row, _DEFINITION_FIELDS)
    definition = ScheduledJobDefinitionV1(
        job_key=_required_job_key(row, "job_key"),
        interval_seconds=_required_positive_int(row, "interval_seconds"),
        lease_ttl_seconds=_required_positive_int(row, "lease_ttl_seconds"),
        max_attempts=_required_positive_int(row, "max_attempts"),
        retry_base_seconds=_required_positive_int(row, "retry_base_seconds"),
        retry_max_seconds=_required_positive_int(row, "retry_max_seconds"),
        max_manual_replays=_required_nonnegative_int(row, "max_manual_replays"),
        enabled=_required_bool(row, "enabled"),
        schema_version=_required_text(row, "schema_version"),
    )
    if _required_sha256(row, "definition_sha256") != definition.definition_sha256:
        raise SchedulerInvariantError("scheduler_definition_digest_mismatch")
    return definition


def _convergence_definition_from_row(
    row: dict[str, object],
) -> ScheduledJobConvergenceDefinitionV1:
    _require_exact_keys(row, _CONVERGENCE_DEFINITION_FIELDS)
    definition_fields = {key: row[key] for key in _DEFINITION_FIELDS}
    scheduler_state = _required_text(row, "scheduler_state")
    if scheduler_state not in {"ready", "blocked"}:
        raise SchedulerInvariantError("scheduler_convergence_definition_state_is_invalid")
    return ScheduledJobConvergenceDefinitionV1(
        definition_id=_required_uuid(row, "definition_id"),
        account_id=_required_account_id(row, "account_id"),
        definition=_definition_from_row(definition_fields),
        revision=_required_positive_int(row, "revision"),
        next_due_at=_required_datetime(row, "next_due_at"),
        scheduler_state=cast(Literal["ready", "blocked"], scheduler_state),
    )


def _run_from_row(row: dict[str, object]) -> ScheduledJobRunV1:
    _require_exact_keys(row, _RUN_FIELDS)
    state = _required_text(row, "state")
    if state not in {"pending", "leased", "retry_wait", "succeeded", "dead_letter"}:
        raise SchedulerInvariantError("scheduler_run_state_is_invalid")
    return ScheduledJobRunV1(
        run_id=_required_uuid(row, "run_id"),
        account_id=_required_account_id(row, "account_id"),
        job_key=_required_job_key(row, "job_key"),
        definition_sha256=_required_sha256(row, "definition_sha256"),
        state=cast(
            Literal["pending", "leased", "retry_wait", "succeeded", "dead_letter"],
            state,
        ),
        revision=_required_positive_int(row, "revision"),
        attempt_count=_required_nonnegative_int(row, "attempt_count"),
        replay_generation=_required_nonnegative_int(row, "replay_generation"),
        replay_of_run_id=_optional_uuid(row, "replay_of_run_id"),
        scheduled_for=_required_datetime(row, "scheduled_for"),
        available_at=_required_datetime(row, "available_at"),
        created_at=_required_datetime(row, "created_at"),
        updated_at=_required_datetime(row, "updated_at"),
    )


def _lease_from_row(row: dict[str, object]) -> ScheduledJobLeaseV1:
    _require_exact_keys(row, _LEASE_FIELDS)
    return ScheduledJobLeaseV1(
        lease_token=_required_uuid(row, "lease_token"),
        run_id=_required_uuid(row, "run_id"),
        account_id=_required_account_id(row, "account_id"),
        holder_id=_required_uuid(row, "holder_id"),
        release_sha=_required_release_sha(row, "release_sha"),
        outer_fencing_token=_required_positive_int(row, "outer_fencing_token"),
        attempt_number=_required_positive_int(row, "attempt_number"),
        run_revision=_required_positive_int(row, "run_revision"),
        leased_at=_required_datetime(row, "leased_at"),
        lease_expires_at=_required_datetime(row, "lease_expires_at"),
    )


def _dead_letter_from_row(
    row: dict[str, object],
    *,
    observed_at: datetime,
) -> SchedulerDeadLetterV1:
    _require_exact_keys(row, _DEAD_LETTER_FIELDS)
    if _required_text(row, "state") != "dead_letter":
        raise SchedulerInvariantError("scheduler_dead_letter_state_is_invalid")
    return SchedulerDeadLetterV1(
        source_run_id=_required_uuid(row, "source_run_id"),
        account_id=_required_account_id(row, "account_id"),
        job_key=_required_job_key(row, "job_key"),
        definition_sha256=_required_sha256(row, "definition_sha256"),
        source_revision=_required_positive_int(row, "source_revision"),
        attempt_count=_required_positive_int(row, "attempt_count"),
        failure_reason_code=_required_reason(row, "failure_reason_code"),
        failure_sha256=_required_sha256(row, "failure_sha256"),
        replay_generation=_required_nonnegative_int(row, "replay_generation"),
        max_manual_replays=_required_nonnegative_int(row, "max_manual_replays"),
        dead_lettered_at=_required_datetime(row, "dead_lettered_at"),
        observed_at=observed_at,
        state="dead_letter",
    )


def _singleton_row(value: object) -> dict[str, object]:
    if type(value) is not list:
        raise SchedulerInvariantError("scheduler_rpc_expected_singleton_row")
    rows = cast(list[object], value)
    if len(rows) != 1:
        raise SchedulerInvariantError("scheduler_rpc_expected_singleton_row")
    return _required_object(rows[0])


def _required_object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise SchedulerInvariantError("scheduler_rpc_expected_object")
    row = cast(dict[object, object], value)
    if not all(type(key) is str for key in row):
        raise SchedulerInvariantError("scheduler_rpc_object_key_is_invalid")
    return cast(dict[str, object], row)


def _require_exact_keys(row: dict[str, object], expected: frozenset[str]) -> None:
    if set(row) != expected:
        raise SchedulerInvariantError("scheduler_rpc_response_fields_are_invalid")


def _required_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if type(value) is not str or not value or value != value.strip():
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_uuid(row: dict[str, object], key: str) -> str:
    value = _required_text(row, key)
    try:
        parsed = UUID(value)
    except ValueError:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid") from None
    if str(parsed) != value:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _optional_uuid(row: dict[str, object], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _required_uuid(row, key)


def _required_account_id(row: dict[str, object], key: str) -> str:
    value = _required_text(row, key)
    if (
        len(value) > 128
        or not value[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value)
    ):
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_job_key(row: dict[str, object], key: str) -> SchedulerJobKey:
    value = _required_text(row, key)
    if value not in {
        "operations.commands",
        "operations.execution",
        "operations.settlement",
        "operations.reconciliation",
        "operations.outbox",
    }:
        raise SchedulerInvariantError("scheduler_rpc_job_key_is_not_allowed")
    return cast(SchedulerJobKey, value)


def _required_sha256(row: dict[str, object], key: str) -> str:
    value = _required_text(row, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_release_sha(row: dict[str, object], key: str) -> str:
    value = _required_text(row, key)
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_reason(row: dict[str, object], key: str) -> str:
    value = _required_text(row, key)
    if (
        len(value) > 128
        or not value[0].islower()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)
    ):
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _optional_reason(row: dict[str, object], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _required_reason(row, key)


def _required_positive_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_nonnegative_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_bool(row: dict[str, object], key: str) -> bool:
    value = row.get(key)
    if type(value) is not bool:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")
    return value


def _required_none(row: dict[str, object], key: str) -> None:
    if row.get(key) is not None:
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid")


def _required_datetime(row: dict[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        raise SchedulerInvariantError("scheduler_rpc_response_field_is_invalid") from None


def _optional_datetime(row: dict[str, object], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _required_datetime(row, key)
