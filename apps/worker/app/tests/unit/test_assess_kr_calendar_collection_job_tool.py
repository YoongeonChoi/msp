from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import cast

import pytest
from pydantic import SecretStr

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobSpecV1,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecoveryAssessmentV1,
)
from app.config import Settings
from app.kr_calendar_collection_runtime import (
    KrCalendarCollectionAssessmentRuntime,
)
from app.tools.assess_kr_calendar_collection_job import (
    KrCalendarCollectionAssessmentCommand,
    parse_command,
    run_command,
)

JOB_ID = "00000000-0000-4000-8000-000000000001"


class AlwaysEqualText(str):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class AlwaysEqualTuple(tuple[str, ...]):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class AlwaysEqualDate(date):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


def _spec() -> KrCalendarCollectionJobSpecV1:
    return KrCalendarCollectionJobSpecV1(
        job_id=JOB_ID,
        provider="toss",
        market="KR",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        trigger="manual",
    )


def _args() -> list[str]:
    return [
        "--job-id",
        JOB_ID,
        "--start-date",
        "2026-07-20",
        "--end-date",
        "2026-07-21",
    ]


def _settings(enabled: bool = True) -> Settings:
    if not enabled:
        return Settings()
    return Settings(
        KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )


def _assessment(classification: str) -> KrCalendarCollectionRecoveryAssessmentV1:
    policies: dict[str, tuple[str, bool, int, int | None]] = {
        "missing": (
            "create_job_by_explicit_manual_invocation",
            True,
            0,
            None,
        ),
        "ready": (
            "advance_next_date_by_explicit_manual_invocation",
            True,
            0,
            1,
        ),
        "paused_retryable": (
            "review_pre_write_failure_before_explicit_manual_invocation",
            True,
            0,
            3,
        ),
        "paused_unrecognized": (
            "investigate_unrecognized_pause_without_retry",
            False,
            0,
            3,
        ),
        "collecting": (
            "investigate_in_flight_attempt_without_retry",
            False,
            0,
            2,
        ),
        "blocked_unknown": (
            "reconcile_unknown_write_outcome_without_retry",
            False,
            0,
            3,
        ),
        "completed": ("no_action_completed", False, 2, 5),
    }
    action, candidate, confirmed_count, revision = policies[classification]
    completed = classification == "completed"
    missing = classification == "missing"
    unresolved_attempt = classification in {"collecting", "blocked_unknown"}
    unresolved_write_outcome = classification in {
        "paused_unrecognized",
        "collecting",
        "blocked_unknown",
    }
    job_state = (
        None
        if missing
        else "paused_retryable"
        if classification == "paused_unrecognized"
        else classification
    )
    state_reason = (
        KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
        if classification == "paused_retryable"
        else "operator_requested_pause"
        if classification == "paused_unrecognized"
        else "collection_write_outcome_unknown"
        if classification == "blocked_unknown"
        else None
    )
    result = object.__new__(KrCalendarCollectionRecoveryAssessmentV1)
    values: dict[str, object] = {
        "schema_version": KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "spec_sha256": _spec().spec_sha256,
        "classification": classification,
        "job_state": job_state,
        "state_reason": state_reason,
        "job_revision": revision,
        "confirmed_date_count": confirmed_count,
        "remaining_date_count": _spec().total_days - confirmed_count,
        "next_date": (
            None if completed else _spec().start_date + (date.resolution * confirmed_count)
        ),
        "unresolved_attempt_present": unresolved_attempt,
        "recommended_operator_action": action,
        "explicit_manual_invocation_candidate": candidate,
        "operator_review_required": not completed,
        "unresolved_write_outcome": unresolved_write_outcome,
        "read_only": True,
        "automatic_retry_allowed": False,
        "mutation_performed": False,
        "mutation_authorized": False,
        "retry_authorized": False,
        "manual_execution_authorized": False,
        "manual_recovery_authorized": False,
        "production_live_authorized": False,
        "full_calendar_certified": False,
        "limitations": KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    return result


class FakeAssessmentService:
    def __init__(
        self,
        result: KrCalendarCollectionRecoveryAssessmentV1 | BaseException,
    ) -> None:
        self.result = result
        self.calls: list[tuple[KrCalendarCollectionJobSpecV1, str]] = []

    async def assess(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        expected_spec_sha256: str,
    ) -> KrCalendarCollectionRecoveryAssessmentV1:
        self.calls.append((spec, expected_spec_sha256))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeRuntime:
    def __init__(self, service: FakeAssessmentService) -> None:
        self.assessment_service = service
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def test_parse_command_fixes_the_canonical_read_only_scope() -> None:
    command = parse_command(_args())

    assert command == KrCalendarCollectionAssessmentCommand(spec=_spec())
    assert command.spec.spec_sha256 == _spec().spec_sha256


@pytest.mark.parametrize(
    "args",
    [
        ["--job-id", "not-a-uuid", "--start-date", "2026-07-20", "--end-date", "2026-07-21"],
        ["--job-id", JOB_ID, "--start-date", "2026-07-21", "--end-date", "2026-07-20"],
    ],
)
def test_parse_command_rejects_invalid_identity_or_range(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_command(args)


async def test_disabled_assessment_fails_before_runtime_factory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_calls = 0

    async def runtime_factory(
        _settings: Settings,
    ) -> KrCalendarCollectionAssessmentRuntime:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("runtime factory must not be called")

    exit_code = await run_command(
        parse_command(_args()),
        settings_loader=lambda: _settings(enabled=False),
        runtime_factory=runtime_factory,
    )

    assert exit_code == 1
    assert factory_calls == 0
    assert capsys.readouterr().out.strip() == (
        "FINAL=FAIL kr_calendar_collection_assessment "
        "read_only=true mutation_performed=false production_order_network_requests=0"
    )


@pytest.mark.parametrize(
    "classification",
    [
        "missing",
        "ready",
        "paused_retryable",
        "paused_unrecognized",
        "collecting",
        "blocked_unknown",
        "completed",
    ],
)
async def test_command_reads_once_and_emits_exact_review_inputs(
    classification: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeAssessmentService(_assessment(classification))
    runtime = FakeRuntime(service)

    async def runtime_factory(
        _settings: Settings,
    ) -> KrCalendarCollectionAssessmentRuntime:
        return cast(KrCalendarCollectionAssessmentRuntime, runtime)

    exit_code = await run_command(
        parse_command(_args()),
        settings_loader=_settings,
        runtime_factory=runtime_factory,
    )
    evidence = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert service.calls == [(_spec(), _spec().spec_sha256)]
    assert runtime.close_calls == 1
    assert evidence["final"] == "PASS"
    assert evidence["classification"] == classification
    assert evidence["spec_sha256"] == _spec().spec_sha256
    assert evidence["state_reason"] == _assessment(classification).state_reason
    assert evidence["read_only"] is True
    assert evidence["mutation_performed"] is False
    assert evidence["production_order_network_requests"] == 0
    if classification in {"missing", "ready", "paused_retryable"}:
        assert evidence["run_precondition"]["expected_classification"] == classification
        assert evidence["run_precondition"]["expected_spec_sha256"] == _spec().spec_sha256
        assert evidence["required_run_confirmation_flags"][0] == ("--confirm-one-date-spec-sha256")
        assert evidence["run_precondition"]["expected_state_reason"] == (
            _assessment(classification).state_reason
        )
    else:
        assert "run_precondition" not in evidence
        assert evidence["required_run_confirmation_flags"] == []
    if classification == "paused_retryable":
        assert evidence["required_run_confirmation_flags"][-1] == (
            "--confirm-reviewed-paused-retryable-spec-sha256"
        )


@pytest.mark.parametrize(
    ("classification", "field_name", "tampered_value"),
    [
        ("ready", "job_id", AlwaysEqualText("secret-job-payload")),
        (
            "ready",
            "recommended_operator_action",
            AlwaysEqualText("secret-action-payload"),
        ),
        (
            "ready",
            "limitations",
            AlwaysEqualTuple(("secret-limitations-payload",)),
        ),
        (
            "ready",
            "limitations",
            (
                AlwaysEqualText(KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS[0]),
                *KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS[1:],
            ),
        ),
        ("ready", "next_date", AlwaysEqualDate(2026, 7, 20)),
        (
            "paused_retryable",
            "state_reason",
            AlwaysEqualText("secret-state-reason-payload"),
        ),
    ],
)
async def test_tampered_assessment_uses_fixed_failure_without_secret(
    classification: str,
    field_name: str,
    tampered_value: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _assessment(classification)
    object.__setattr__(result, field_name, tampered_value)
    runtime = FakeRuntime(FakeAssessmentService(result))

    async def runtime_factory(
        _settings: Settings,
    ) -> KrCalendarCollectionAssessmentRuntime:
        return cast(KrCalendarCollectionAssessmentRuntime, runtime)

    exit_code = await run_command(
        parse_command(_args()),
        settings_loader=_settings,
        runtime_factory=runtime_factory,
    )
    output = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert runtime.close_calls == 1
    assert output.startswith("FINAL=FAIL kr_calendar_collection_assessment")
    assert "secret" not in output


async def test_cancellation_propagates_after_assessment_runtime_closes() -> None:
    runtime = FakeRuntime(FakeAssessmentService(asyncio.CancelledError()))

    async def runtime_factory(
        _settings: Settings,
    ) -> KrCalendarCollectionAssessmentRuntime:
        return cast(KrCalendarCollectionAssessmentRuntime, runtime)

    with pytest.raises(asyncio.CancelledError):
        await run_command(
            parse_command(_args()),
            settings_loader=_settings,
            runtime_factory=runtime_factory,
        )

    assert runtime.close_calls == 1
