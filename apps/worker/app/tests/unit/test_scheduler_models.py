from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any, cast

import pytest

from app.domain.scheduler.models import (
    ScheduledJobClaimV1,
    ScheduledJobConvergenceDefinitionV1,
    ScheduledJobDefinitionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDeadLetterV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerReplayAssessmentV1,
    scheduler_definition_budget_is_safe,
    scheduler_replay_budget_is_safe,
    scheduler_result_sha256,
    scheduler_retry_delay,
)

NOW = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)
RUN_ID = "11111111-1111-4111-8111-111111111111"
LEASE_TOKEN = "22222222-2222-4222-8222-222222222222"
HOLDER_ID = "33333333-3333-4333-8333-333333333333"
PARENT_RUN_ID = "44444444-4444-4444-8444-444444444444"
DEFINITION_ID = "55555555-5555-4555-8555-555555555555"
RELEASE_SHA = "a" * 40
FAILURE_SHA = "b" * 64


def test_scheduler_definition_digest_is_stable_and_field_sensitive() -> None:
    first = _definition()
    second = _definition()
    changed = _definition(interval_seconds=3)

    assert first.definition_sha256 == second.definition_sha256
    assert (
        first.definition_sha256
        == "11445f386e48597f05c47be98288e4aeebf2847f4ab5e329bacae5487ec5cf82"
    )
    assert first.definition_sha256 != changed.definition_sha256
    assert len(first.definition_sha256) == 64


def test_scheduler_retry_receipt_preserves_original_transition_schedule() -> None:
    definition = _definition()
    receipt = ScheduledJobFailureReceiptV1(
        run_id=RUN_ID,
        run_revision=3,
        attempt_count=1,
        state="retry_wait",
        failure_reason_code="command_poll_retryable",
        result_sha256=FAILURE_SHA,
        next_attempt_at=NOW + scheduler_retry_delay(definition, 1),
        observed_at=NOW + timedelta(seconds=30),
    )

    assert receipt.next_attempt_at is not None
    assert receipt.next_attempt_at < receipt.observed_at
    assert scheduler_retry_delay(definition, 4) == timedelta(seconds=8)


def test_scheduler_budget_policy_separates_effectful_and_safe_jobs() -> None:
    assert scheduler_definition_budget_is_safe(
        _definition(max_attempts=3, max_manual_replays=1)
    )
    assert not scheduler_definition_budget_is_safe(
        _definition(max_attempts=4, max_manual_replays=1)
    )
    effectful = _definition(
        job_key="operations.execution",
        max_attempts=1,
        max_manual_replays=0,
    )
    assert scheduler_definition_budget_is_safe(effectful)
    assert not scheduler_definition_budget_is_safe(
        _definition(
            job_key="operations.execution",
            max_attempts=2,
            max_manual_replays=0,
        )
    )
    assert scheduler_replay_budget_is_safe(_dead_letter(max_manual_replays=1))
    assert not scheduler_replay_budget_is_safe(_dead_letter(max_manual_replays=2))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_key", "operations.dynamic"),
        ("interval_seconds", True),
        ("lease_ttl_seconds", 0),
        ("lease_ttl_seconds", 9),
        ("max_attempts", 101),
        ("retry_base_seconds", 10),
        ("max_manual_replays", -1),
        ("enabled", 1),
        ("schema_version", "v2"),
    ],
)
def test_scheduler_definition_rejects_invalid_contract(field: str, value: object) -> None:
    values: dict[str, object] = {
        "job_key": "operations.commands",
        "interval_seconds": 2,
        "lease_ttl_seconds": 30,
        "max_attempts": 4,
        "retry_base_seconds": 2,
        "retry_max_seconds": 8,
        "max_manual_replays": 1,
        "enabled": True,
        "schema_version": "durable_scheduler_job_definition.v1",
    }
    values[field] = value
    if field == "retry_base_seconds":
        values["retry_max_seconds"] = 8

    with pytest.raises(SchedulerInvariantError):
        ScheduledJobDefinitionV1(**cast(Any, values))


def test_claim_binds_definition_run_lease_and_database_clock() -> None:
    claim = _claim()

    assert claim.job_key == "operations.commands"
    assert claim.observed_at == NOW
    assert claim.lease.lease_expires_at == NOW + timedelta(seconds=30)


def test_claim_rejects_release_sha_uuid_time_and_binding_invalids() -> None:
    with pytest.raises(SchedulerInvariantError, match="release_sha"):
        _lease(release_sha="A" * 40)
    with pytest.raises(SchedulerInvariantError, match="holder_id"):
        _lease(holder_id="not-a-uuid")
    with pytest.raises(SchedulerInvariantError, match="timezone"):
        _run(created_at=NOW.replace(tzinfo=None))
    with pytest.raises(SchedulerInvariantError, match="attempt_number_mismatch"):
        ScheduledJobClaimV1(
            definition=_definition(),
            run=_run(),
            lease=_lease(attempt_number=2),
            observed_at=NOW,
        )


def test_claim_enforces_attempt_replay_and_inner_lease_budgets() -> None:
    attempt_definition = _definition(max_attempts=1)
    with pytest.raises(SchedulerInvariantError, match="attempt_budget"):
        ScheduledJobClaimV1(
            definition=attempt_definition,
            run=_run(
                attempt_count=2,
                definition_sha256=attempt_definition.definition_sha256,
            ),
            lease=_lease(attempt_number=2),
            observed_at=NOW,
        )
    replay_definition = _definition(max_manual_replays=1)
    with pytest.raises(SchedulerInvariantError, match="replay_budget"):
        ScheduledJobClaimV1(
            definition=replay_definition,
            run=_run(
                replay_generation=2,
                replay_of_run_id=PARENT_RUN_ID,
                definition_sha256=replay_definition.definition_sha256,
            ),
            lease=_lease(),
            observed_at=NOW,
        )
    with pytest.raises(SchedulerInvariantError, match="lease_ttl"):
        ScheduledJobClaimV1(
            definition=_definition(lease_ttl_seconds=30),
            run=_run(),
            lease=_lease(lease_expires_at=NOW + timedelta(seconds=31)),
            observed_at=NOW,
        )
    with pytest.raises(SchedulerInvariantError, match="database_clock"):
        ScheduledJobClaimV1(
            definition=_definition(),
            run=_run(),
            lease=_lease(),
            observed_at=NOW + timedelta(seconds=30),
        )
    with pytest.raises(SchedulerInvariantError, match="lease_time"):
        ScheduledJobClaimV1(
            definition=_definition(),
            run=_run(),
            lease=_lease(
                leased_at=NOW - timedelta(microseconds=1),
                lease_expires_at=NOW + timedelta(seconds=29),
            ),
            observed_at=NOW,
        )
    with pytest.raises(SchedulerInvariantError, match="run_update"):
        ScheduledJobClaimV1(
            definition=_definition(),
            run=_run(updated_at=NOW - timedelta(microseconds=1)),
            lease=_lease(),
            observed_at=NOW,
        )
    with pytest.raises(SchedulerInvariantError, match="not_available"):
        ScheduledJobClaimV1(
            definition=_definition(),
            run=_run(available_at=NOW + timedelta(microseconds=1)),
            lease=_lease(),
            observed_at=NOW,
        )


def test_overdue_definition_receipt_keeps_db_clock_evidence() -> None:
    receipt = ScheduledJobDefinitionReceiptV1(
        definition_id=DEFINITION_ID,
        account_id="paper-primary",
        job_key="operations.commands",
        definition_sha256=_definition().definition_sha256,
        revision=1,
        next_due_at=NOW - timedelta(minutes=5),
        observed_at=NOW,
    )

    assert receipt.next_due_at < receipt.observed_at


def test_scheduler_time_fields_reject_subclasses_overflow_and_timezone_errors() -> None:
    class DatetimeSubclass(datetime):
        pass

    class ExplodingTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> timedelta:
            raise RuntimeError("timezone-secret")

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, _value: datetime | None) -> str:
            return "exploding"

    invalid_values = (
        DatetimeSubclass(2026, 7, 24, 3, 0, tzinfo=UTC),
        datetime.min.replace(
            tzinfo=timezone(timedelta(hours=23, minutes=59)),
        ),
        datetime(2026, 7, 24, 3, 0, tzinfo=ExplodingTimezone()),
    )

    for invalid in invalid_values:
        with pytest.raises(SchedulerInvariantError, match="timezone") as captured:
            ScheduledJobDefinitionReceiptV1(
                definition_id=DEFINITION_ID,
                account_id="paper-primary",
                job_key="operations.commands",
                definition_sha256=_definition().definition_sha256,
                revision=1,
                next_due_at=invalid,
                observed_at=NOW,
            )
        assert "timezone-secret" not in str(captured.value)


def test_replay_assessment_is_bound_to_exact_dead_letter_evidence() -> None:
    assessment = _assessment()

    assert assessment == _assessment()
    with pytest.raises(SchedulerInvariantError, match="exceeds_budget"):
        SchedulerReplayAssessmentV1(
            dead_letter=_dead_letter(replay_generation=1, max_manual_replays=1),
            eligible=True,
            ineligibility_reason=None,
        )
    with pytest.raises(SchedulerInvariantError, match="effectful"):
        SchedulerReplayAssessmentV1(
            dead_letter=_dead_letter(job_key="operations.execution"),
            eligible=True,
            ineligibility_reason=None,
        )


def test_convergence_receipt_enforces_exact_status_field_coherence() -> None:
    definition = _definition()
    state = ScheduledJobConvergenceDefinitionV1(
        definition_id=DEFINITION_ID,
        account_id="paper-primary",
        definition=definition,
        revision=1,
        next_due_at=NOW,
        scheduler_state="ready",
    )
    converged = SchedulerDefinitionConvergenceReceiptV1(
        status="converged",
        definition=state,
        claim=None,
        active_run_id=None,
        next_eligible_at=None,
        reason_code=None,
        observed_at=NOW,
    )

    assert converged.status == "converged"
    with pytest.raises(SchedulerInvariantError, match="not_quiescent"):
        SchedulerDefinitionConvergenceReceiptV1(
            status="converged",
            definition=state,
            claim=None,
            active_run_id=RUN_ID,
            next_eligible_at=None,
            reason_code=None,
            observed_at=NOW,
        )


@pytest.mark.parametrize("scheduler_state", ["ready", "blocked"])
def test_convergence_receipt_never_accepts_effectful_claim(
    scheduler_state: str,
) -> None:
    definition = _definition(job_key="operations.execution")
    run = _run(
        job_key=definition.job_key,
        definition_sha256=definition.definition_sha256,
    )
    claim = ScheduledJobClaimV1(
        definition=definition,
        run=run,
        lease=_lease(),
        observed_at=NOW,
    )
    state = ScheduledJobConvergenceDefinitionV1(
        definition_id=DEFINITION_ID,
        account_id="paper-primary",
        definition=definition,
        revision=1,
        next_due_at=NOW,
        scheduler_state=cast(Any, scheduler_state),
    )

    with pytest.raises(SchedulerInvariantError, match="claimed_convergence"):
        SchedulerDefinitionConvergenceReceiptV1(
            status="claimed",
            definition=state,
            claim=claim,
            active_run_id=run.run_id,
            next_eligible_at=None,
            reason_code=None,
            observed_at=NOW,
        )


def test_convergence_receipt_never_claims_from_blocked_safe_definition() -> None:
    definition = _definition()
    claim = _claim()
    state = ScheduledJobConvergenceDefinitionV1(
        definition_id=DEFINITION_ID,
        account_id="paper-primary",
        definition=definition,
        revision=1,
        next_due_at=NOW,
        scheduler_state="blocked",
    )

    with pytest.raises(SchedulerInvariantError, match="claimed_convergence"):
        SchedulerDefinitionConvergenceReceiptV1(
            status="claimed",
            definition=state,
            claim=claim,
            active_run_id=claim.run.run_id,
            next_eligible_at=None,
            reason_code=None,
            observed_at=NOW,
        )
    with pytest.raises(SchedulerInvariantError, match="waiting"):
        SchedulerDefinitionConvergenceReceiptV1(
            status="wait",
            definition=state,
            claim=None,
            active_run_id=RUN_ID,
            next_eligible_at=NOW,
            reason_code="scheduler_retry_wait",
            observed_at=NOW,
        )


def test_scheduler_result_digest_is_canonical_and_rejects_unsafe_objects() -> None:
    @dataclass(frozen=True)
    class Result:
        applied: int
        failed: int

    assert scheduler_result_sha256(Result(2, 0)) == scheduler_result_sha256(
        {"applied": 2, "failed": 0}
    )
    assert scheduler_result_sha256({"b": 2, "a": 1}) == scheduler_result_sha256({"a": 1, "b": 2})
    with pytest.raises(SchedulerInvariantError, match="canonical_json"):
        scheduler_result_sha256(1.5)
    with pytest.raises(SchedulerInvariantError, match="canonical_json"):
        scheduler_result_sha256(object())


def test_scheduler_result_digest_enforces_resource_and_cycle_bounds() -> None:
    recursive: list[object] = []
    recursive.append(recursive)
    too_deep: object = 0
    for _ in range(10):
        too_deep = [too_deep]

    with pytest.raises(SchedulerInvariantError, match="cycle"):
        scheduler_result_sha256(recursive)
    with pytest.raises(SchedulerInvariantError, match="depth"):
        scheduler_result_sha256(too_deep)
    with pytest.raises(SchedulerInvariantError, match="node_count"):
        scheduler_result_sha256(list(range(256)))
    with pytest.raises(SchedulerInvariantError, match="string_length"):
        scheduler_result_sha256("x" * 4_097)
    with pytest.raises(SchedulerInvariantError, match="out_of_range"):
        scheduler_result_sha256(2**63)
    with pytest.raises(SchedulerInvariantError, match="serialized_size"):
        scheduler_result_sha256(["한" * 4_096, "나" * 4_096])


def _definition(**changes: object) -> ScheduledJobDefinitionV1:
    values: dict[str, object] = {
        "job_key": "operations.commands",
        "interval_seconds": 2,
        "lease_ttl_seconds": 30,
        "max_attempts": 4,
        "retry_base_seconds": 2,
        "retry_max_seconds": 8,
        "max_manual_replays": 1,
        "enabled": True,
    }
    values.update(changes)
    return ScheduledJobDefinitionV1(**cast(Any, values))


def _run(**changes: object) -> ScheduledJobRunV1:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "account_id": "paper-primary",
        "job_key": "operations.commands",
        "definition_sha256": _definition().definition_sha256,
        "state": "leased",
        "revision": 2,
        "attempt_count": 1,
        "replay_generation": 0,
        "replay_of_run_id": None,
        "scheduled_for": NOW - timedelta(seconds=2),
        "available_at": NOW - timedelta(seconds=2),
        "created_at": NOW - timedelta(seconds=2),
        "updated_at": NOW,
    }
    values.update(changes)
    return ScheduledJobRunV1(**cast(Any, values))


def _lease(**changes: object) -> ScheduledJobLeaseV1:
    values: dict[str, object] = {
        "lease_token": LEASE_TOKEN,
        "run_id": RUN_ID,
        "account_id": "paper-primary",
        "holder_id": HOLDER_ID,
        "release_sha": RELEASE_SHA,
        "outer_fencing_token": 7,
        "attempt_number": 1,
        "run_revision": 2,
        "leased_at": NOW,
        "lease_expires_at": NOW + timedelta(seconds=30),
    }
    values.update(changes)
    return ScheduledJobLeaseV1(**cast(Any, values))


def _claim() -> ScheduledJobClaimV1:
    return ScheduledJobClaimV1(
        definition=_definition(),
        run=_run(),
        lease=_lease(),
        observed_at=NOW,
    )


def _dead_letter(**changes: object) -> SchedulerDeadLetterV1:
    values: dict[str, object] = {
        "source_run_id": PARENT_RUN_ID,
        "account_id": "paper-primary",
        "job_key": "operations.commands",
        "definition_sha256": _definition().definition_sha256,
        "source_revision": 3,
        "attempt_count": 4,
        "failure_reason_code": "scheduler_handler_unknown_failure",
        "failure_sha256": FAILURE_SHA,
        "replay_generation": 0,
        "max_manual_replays": 1,
        "dead_lettered_at": NOW - timedelta(seconds=1),
        "observed_at": NOW,
    }
    values.update(changes)
    return SchedulerDeadLetterV1(**cast(Any, values))


def _assessment() -> SchedulerReplayAssessmentV1:
    return SchedulerReplayAssessmentV1(
        dead_letter=_dead_letter(),
        eligible=True,
        ineligibility_reason=None,
    )
