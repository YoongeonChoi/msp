from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobSpecV1,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecoveryAssessmentV1,
)
from app.config import Settings, load_settings
from app.kr_calendar_collection_runtime import (
    KrCalendarCollectionAssessmentRuntime,
    build_kr_calendar_collection_assessment_runtime,
)

_SAFE_FAILURE_LINE = (
    "FINAL=FAIL kr_calendar_collection_assessment "
    "read_only=true mutation_performed=false production_order_network_requests=0"
)
_EVIDENCE_SCHEMA_VERSION = "kr_calendar_collection_assessment_evidence.v1"
_STATE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


@dataclass(frozen=True, slots=True)
class KrCalendarCollectionAssessmentCommand:
    spec: KrCalendarCollectionJobSpecV1


def parse_command(
    argv: Sequence[str] | None = None,
) -> KrCalendarCollectionAssessmentCommand:
    parser = argparse.ArgumentParser(
        description="Read one durable KR calendar job and print its safe review inputs.",
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--start-date", required=True, type=_iso_date)
    parser.add_argument("--end-date", required=True, type=_iso_date)
    values = parser.parse_args(argv)
    try:
        spec = KrCalendarCollectionJobSpecV1(
            job_id=values.job_id,
            provider="toss",
            market="KR",
            start_date=values.start_date,
            end_date=values.end_date,
            trigger="manual",
        )
    except Exception:
        parser.error("calendar collection job identity or date range is invalid")
    return KrCalendarCollectionAssessmentCommand(spec=spec)


async def run_command(
    command: KrCalendarCollectionAssessmentCommand,
    *,
    settings_loader: Callable[[], Settings] = load_settings,
    runtime_factory: Callable[
        [Settings], Awaitable[KrCalendarCollectionAssessmentRuntime]
    ] = build_kr_calendar_collection_assessment_runtime,
) -> int:
    try:
        spec = _validate_command(command)
        settings = settings_loader()
        settings.require_kr_calendar_collection_assessment()
        runtime = await runtime_factory(settings)
        try:
            result = await runtime.assessment_service.assess(
                spec,
                expected_spec_sha256=spec.spec_sha256,
            )
            evidence = _assessment_evidence(spec, result)
            output = json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        finally:
            await runtime.close()
    except Exception:
        print(_SAFE_FAILURE_LINE)
        return 1

    print(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    command = parse_command(argv)
    return asyncio.run(run_command(command))


def _validate_command(
    command: object,
) -> KrCalendarCollectionJobSpecV1:
    if type(command) is not KrCalendarCollectionAssessmentCommand:
        raise ValueError("kr_calendar_collection_assessment_command_invalid")
    spec = command.spec
    if type(spec) is not KrCalendarCollectionJobSpecV1:
        raise ValueError("kr_calendar_collection_assessment_command_invalid")
    try:
        canonical = KrCalendarCollectionJobSpecV1(
            job_id=spec.job_id,
            provider=spec.provider,
            market=spec.market,
            start_date=spec.start_date,
            end_date=spec.end_date,
            trigger=spec.trigger,
            schema_version=spec.schema_version,
        )
    except Exception:
        raise ValueError("kr_calendar_collection_assessment_command_invalid") from None
    if (
        canonical != spec
        or canonical.provider != "toss"
        or canonical.market != "KR"
        or canonical.trigger != "manual"
    ):
        raise ValueError("kr_calendar_collection_assessment_command_invalid")
    return canonical


def _assessment_evidence(
    spec: KrCalendarCollectionJobSpecV1,
    result: KrCalendarCollectionRecoveryAssessmentV1,
) -> dict[str, object]:
    if type(result) is not KrCalendarCollectionRecoveryAssessmentV1:
        raise ValueError("kr_calendar_collection_assessment_result_invalid")
    classification = result.classification
    policies: dict[str, tuple[str, bool]] = {
        "missing": ("create_job_by_explicit_manual_invocation", True),
        "ready": ("advance_next_date_by_explicit_manual_invocation", True),
        "paused_retryable": (
            "review_pre_write_failure_before_explicit_manual_invocation",
            True,
        ),
        "paused_unrecognized": (
            "investigate_unrecognized_pause_without_retry",
            False,
        ),
        "collecting": (
            "investigate_in_flight_attempt_without_retry",
            False,
        ),
        "blocked_unknown": (
            "reconcile_unknown_write_outcome_without_retry",
            False,
        ),
        "completed": ("no_action_completed", False),
    }
    if type(classification) is not str or classification not in policies:
        raise ValueError("kr_calendar_collection_assessment_result_invalid")
    action, explicit_candidate = policies[classification]
    unresolved_attempt = classification in {"collecting", "blocked_unknown"}
    unresolved_write_outcome = classification in {
        "paused_unrecognized",
        "collecting",
        "blocked_unknown",
    }
    confirmed_count = result.confirmed_date_count
    if type(confirmed_count) is not int or not 0 <= confirmed_count <= spec.total_days:
        raise ValueError("kr_calendar_collection_assessment_result_invalid")
    missing = classification == "missing"
    completed = classification == "completed"
    expected_next_date = None if completed else spec.start_date + timedelta(days=confirmed_count)
    revision_valid = (
        result.job_revision is None
        if missing
        else type(result.job_revision) is int and result.job_revision > 0
    )
    expected_job_state = (
        "paused_retryable" if classification == "paused_unrecognized" else classification
    )
    state_reason = result.state_reason
    if classification == "paused_retryable":
        state_reason_valid = (
            type(state_reason) is str
            and state_reason == KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
        )
    elif classification in {"paused_unrecognized", "blocked_unknown"}:
        state_reason_valid = (
            type(state_reason) is str
            and _STATE_REASON_RE.fullmatch(state_reason) is not None
            and (
                classification != "paused_unrecognized"
                or state_reason != KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
            )
        )
    else:
        state_reason_valid = state_reason is None
    count_valid = (
        confirmed_count == 0
        if missing
        else confirmed_count == spec.total_days
        if completed
        else confirmed_count < spec.total_days
    )
    if (
        type(result.schema_version) is not str
        or type(result.job_id) is not str
        or type(result.spec_sha256) is not str
        or (missing and result.job_state is not None)
        or (not missing and type(result.job_state) is not str)
        or (completed and result.next_date is not None)
        or (not completed and type(result.next_date) is not date)
        or type(result.recommended_operator_action) is not str
        or type(result.limitations) is not tuple
        or any(type(value) is not str for value in result.limitations)
    ):
        raise ValueError("kr_calendar_collection_assessment_result_invalid")
    if not all(
        (
            result.schema_version == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
            result.job_id == spec.job_id,
            result.spec_sha256 == spec.spec_sha256,
            revision_valid,
            result.job_state == (None if missing else expected_job_state),
            state_reason_valid,
            count_valid,
            type(result.remaining_date_count) is int,
            result.remaining_date_count == spec.total_days - confirmed_count,
            result.next_date == expected_next_date,
            result.unresolved_attempt_present is unresolved_attempt,
            result.recommended_operator_action == action,
            result.explicit_manual_invocation_candidate is explicit_candidate,
            result.operator_review_required is (not completed),
            result.unresolved_write_outcome is unresolved_write_outcome,
            result.read_only is True,
            result.automatic_retry_allowed is False,
            result.mutation_performed is False,
            result.mutation_authorized is False,
            result.retry_authorized is False,
            result.manual_execution_authorized is False,
            result.manual_recovery_authorized is False,
            result.production_live_authorized is False,
            result.full_calendar_certified is False,
            result.limitations == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
        )
    ):
        raise ValueError("kr_calendar_collection_assessment_result_invalid")

    required_confirmation_flags: list[str] = []
    run_precondition: dict[str, object] | None = None
    if explicit_candidate:
        if expected_next_date is None:
            raise ValueError("kr_calendar_collection_assessment_result_invalid")
        run_precondition = {
            "expected_classification": classification,
            "expected_confirmed_count": confirmed_count,
            "expected_next_date": expected_next_date.isoformat(),
            "expected_revision": result.job_revision,
            "expected_spec_sha256": result.spec_sha256,
            "expected_state_reason": result.state_reason,
        }
        required_confirmation_flags.append("--confirm-one-date-spec-sha256")
        if classification == "paused_retryable":
            required_confirmation_flags.append("--confirm-reviewed-paused-retryable-spec-sha256")

    evidence: dict[str, object] = {
        "automatic_retry_allowed": result.automatic_retry_allowed,
        "classification": classification,
        "confirmed_date_count": confirmed_count,
        "explicit_manual_invocation_candidate": explicit_candidate,
        "final": "PASS",
        "full_calendar_certified": result.full_calendar_certified,
        "job_id": result.job_id,
        "job_revision": result.job_revision,
        "job_state": result.job_state,
        "state_reason": result.state_reason,
        "limitations": list(result.limitations),
        "manual_execution_authorized": result.manual_execution_authorized,
        "manual_recovery_authorized": result.manual_recovery_authorized,
        "mutation_performed": result.mutation_performed,
        "next_date": (None if result.next_date is None else result.next_date.isoformat()),
        "operator_review_required": result.operator_review_required,
        "production_live_authorized": result.production_live_authorized,
        "production_order_network_requests": 0,
        "read_only": result.read_only,
        "recommended_operator_action": result.recommended_operator_action,
        "remaining_date_count": result.remaining_date_count,
        "required_run_confirmation_flags": required_confirmation_flags,
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "spec_sha256": result.spec_sha256,
        "unresolved_attempt_present": result.unresolved_attempt_present,
        "unresolved_write_outcome": result.unresolved_write_outcome,
    }
    if run_precondition is not None:
        evidence["run_precondition"] = run_precondition
    return evidence


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be an exact YYYY-MM-DD date")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
