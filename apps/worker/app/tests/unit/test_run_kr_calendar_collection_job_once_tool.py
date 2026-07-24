from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import pytest
from pydantic import SecretStr

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobSpecV1,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
)
from app.application.use_cases.run_kr_calendar_date_range_collection_job import (
    KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS,
    KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION,
    KrCalendarDateRangeCollectionRunResultV1,
)
from app.config import Settings
from app.domain.market_data.point_in_time_calendar import (
    kr_daily_session_idempotency_key,
)
from app.kr_calendar_collection_runtime import KrCalendarCollectionRuntime
from app.tools.run_kr_calendar_collection_job_once import (
    parse_command,
    run_command,
)

JOB_ID = "00000000-0000-4000-8000-000000000001"
HOLDER_ID = "00000000-0000-4000-8000-000000000099"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000002"
OCCURRENCE_ID = "00000000-0000-4000-8000-000000000003"


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


def _args(
    classification: str = "missing",
    *,
    revision: int | None = None,
    confirmed_count: int = 0,
    next_date: date = date(2026, 7, 20),
    expected_sha256: str | None = None,
    confirmation_sha256: str | None = None,
    paused_confirmation_sha256: str | None = None,
    state_reason: str | None = None,
) -> list[str]:
    spec_sha256 = _spec().spec_sha256
    args = [
        "--job-id",
        JOB_ID,
        "--start-date",
        "2026-07-20",
        "--end-date",
        "2026-07-21",
        "--expected-spec-sha256",
        expected_sha256 or spec_sha256,
        "--expected-classification",
        classification,
        "--expected-confirmed-count",
        str(confirmed_count),
        "--expected-next-date",
        next_date.isoformat(),
        "--confirm-one-date-spec-sha256",
        confirmation_sha256 or spec_sha256,
    ]
    if revision is not None:
        args.extend(("--expected-revision", str(revision)))
    if classification == "paused_retryable" and state_reason is None:
        state_reason = KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
    if state_reason is not None:
        args.extend(("--expected-state-reason", state_reason))
    if paused_confirmation_sha256 is not None:
        args.extend(
            (
                "--confirm-reviewed-paused-retryable-spec-sha256",
                paused_confirmation_sha256,
            )
        )
    return args


def _settings(enabled: bool = True) -> Settings:
    if not enabled:
        return Settings()
    return Settings(
        KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED=True,
        KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED=True,
        KR_CALENDAR_COLLECTION_HOLDER_ID=HOLDER_ID,
        MOCK_PROVIDERS=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
        TOSS_CLIENT_ID=SecretStr("dummy-client-id"),
        TOSS_CLIENT_SECRET=SecretStr("dummy-client-secret"),
        TOSS_CREDENTIAL_SCOPE="read_only",
        TOSS_ORDER_CAPABLE_CREDENTIALS=False,
    )


def _result() -> KrCalendarDateRangeCollectionRunResultV1:
    observed_at = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
    result = object.__new__(KrCalendarDateRangeCollectionRunResultV1)
    values: dict[str, object] = {
        "schema_version": KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "spec_sha256": _spec().spec_sha256,
        "provider": "toss",
        "market": "KR",
        "action": "advanced",
        "processed_date": date(2026, 7, 20),
        "job_state": "ready",
        "job_revision": 3,
        "total_date_count": 2,
        "confirmed_date_count": 1,
        "remaining_date_count": 1,
        "terminal_manifest_sha256": None,
        "processed_checkpoint_attempt_id": ATTEMPT_ID,
        "processed_checkpoint_holder_id": HOLDER_ID,
        "processed_checkpoint_fencing_revision": 2,
        "processed_checkpoint_begun_at": observed_at,
        "processed_checkpoint_confirmed_at": observed_at,
        "processed_receipt_status": "stored",
        "processed_receipt_calendar_idempotency_key": (
            kr_daily_session_idempotency_key(
                provider="toss",
                market="KR",
                session_date=date(2026, 7, 20),
            )
        ),
        "processed_receipt_canonical_evidence_sha256": "2" * 64,
        "processed_receipt_revision": 1,
        "processed_receipt_revision_inserted": True,
        "processed_receipt_occurrence_id": OCCURRENCE_ID,
        "processed_receipt_occurrence_inserted": True,
        "processed_receipt_observed_at": observed_at,
        "manual_execution_only": True,
        "automatic_retry_allowed": False,
        "durable_runtime_configured": True,
        "full_calendar_certified": False,
        "limitations": KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    return result


class FakeRunner:
    def __init__(
        self,
        result: KrCalendarDateRangeCollectionRunResultV1 | BaseException,
    ) -> None:
        self.result = result
        self.calls: list[tuple[KrCalendarCollectionJobSpecV1, dict[str, object]]] = []

    async def execute(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        **kwargs: object,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        self.calls.append((spec, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeRuntime:
    def __init__(self, runner: FakeRunner) -> None:
        self.runner = runner
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def test_parse_command_fixes_provider_market_trigger_and_missing_precondition() -> None:
    command = parse_command(_args())

    assert command.spec == _spec()
    assert command.expected_spec_sha256 == _spec().spec_sha256
    assert command.expected_classification == "missing"
    assert command.expected_revision is None
    assert command.expected_confirmed_count == 0
    assert command.expected_next_date == _spec().start_date
    assert command.expected_state_reason is None
    assert command.manual_confirmation_spec_sha256 == _spec().spec_sha256
    assert command.paused_retry_confirmation_spec_sha256 is None


def test_parse_command_requires_exact_ready_and_paused_preconditions() -> None:
    ready = parse_command(
        _args(
            "ready",
            revision=3,
            confirmed_count=1,
            next_date=date(2026, 7, 21),
        )
    )
    paused = parse_command(
        _args(
            "paused_retryable",
            revision=4,
            confirmed_count=1,
            next_date=date(2026, 7, 21),
            paused_confirmation_sha256=_spec().spec_sha256,
        )
    )

    assert ready.expected_revision == 3
    assert ready.paused_retry_confirmation_spec_sha256 is None
    assert paused.expected_revision == 4
    assert paused.expected_state_reason == KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
    assert paused.paused_retry_confirmation_spec_sha256 == _spec().spec_sha256


@pytest.mark.parametrize(
    "args",
    [
        _args(expected_sha256="0" * 64),
        _args(confirmation_sha256="0" * 64),
        _args("missing", revision=1),
        _args("ready"),
        _args(
            "ready",
            revision=2,
            confirmed_count=1,
            next_date=date(2026, 7, 20),
        ),
        _args(
            "paused_retryable",
            revision=2,
            paused_confirmation_sha256=None,
        ),
        _args(
            "paused_retryable",
            revision=2,
            paused_confirmation_sha256=_spec().spec_sha256,
            state_reason="operator_requested_pause",
        ),
    ],
)
def test_parse_command_rejects_invalid_authorization_before_runtime(
    args: list[str],
) -> None:
    with pytest.raises(SystemExit):
        parse_command(args)


async def test_disabled_config_fails_before_runtime_factory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse_command(_args())
    factory_calls = 0

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("runtime factory must not be called")

    exit_code = await run_command(
        command,
        settings_loader=lambda: _settings(enabled=False),
        runtime_factory=runtime_factory,
    )

    assert exit_code == 1
    assert factory_calls == 0
    assert capsys.readouterr().out.strip() == (
        "FINAL=FAIL kr_calendar_collection_manual_once production_order_network_requests=0"
    )


async def test_forged_command_fails_before_settings_or_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = replace(
        parse_command(_args()),
        expected_spec_sha256="0" * 64,
    )
    settings_loads = 0
    factory_calls = 0

    def settings_loader() -> Settings:
        nonlocal settings_loads
        settings_loads += 1
        raise AssertionError("settings must not be loaded")

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("runtime factory must not be called")

    exit_code = await run_command(
        command,
        settings_loader=settings_loader,
        runtime_factory=runtime_factory,
    )

    assert exit_code == 1
    assert settings_loads == 0
    assert factory_calls == 0
    assert capsys.readouterr().out.strip() == (
        "FINAL=FAIL kr_calendar_collection_manual_once production_order_network_requests=0"
    )


async def test_command_runs_once_closes_and_emits_structured_safe_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse_command(_args())
    runner = FakeRunner(_result())
    runtime = FakeRuntime(runner)
    factory_calls = 0

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        nonlocal factory_calls
        factory_calls += 1
        return cast(KrCalendarCollectionRuntime, runtime)

    exit_code = await run_command(
        command,
        settings_loader=_settings,
        runtime_factory=runtime_factory,
    )
    evidence = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert factory_calls == 1
    assert len(runner.calls) == 1
    assert runner.calls[0] == (
        _spec(),
        {
            "expected_spec_sha256": _spec().spec_sha256,
            "expected_classification": "missing",
            "expected_revision": None,
            "expected_confirmed_count": 0,
            "expected_next_date": date(2026, 7, 20),
            "expected_state_reason": None,
            "manual_confirmation": True,
            "paused_retry_confirmation": False,
        },
    )
    assert runtime.close_calls == 1
    assert evidence["final"] == "PASS"
    assert evidence["schema_version"] == ("kr_calendar_collection_manual_run_evidence.v1")
    assert evidence["job_id"] == JOB_ID
    assert evidence["spec_sha256"] == _spec().spec_sha256
    assert evidence["expected_precondition"] == {
        "confirmed_date_count": 0,
        "job_revision": None,
        "next_date": "2026-07-20",
        "state_reason": None,
    }
    assert evidence["processed_date"] == "2026-07-20"
    assert evidence["processed_checkpoint_holder_id"] == HOLDER_ID
    assert evidence["processed_receipt_status"] == "stored"
    assert evidence["production_order_network_requests"] == 0
    assert evidence["limitations"] == list(KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS)


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("schema_version", "forged.v1"),
        ("provider", AlwaysEqualText("secret-provider-payload")),
        ("processed_date", AlwaysEqualDate(2026, 7, 20)),
        ("processed_date", date(2026, 7, 21)),
        ("confirmed_date_count", 0),
        ("remaining_date_count", 2),
        ("job_revision", 4),
        ("processed_checkpoint_fencing_revision", 3),
        (
            "processed_checkpoint_holder_id",
            "00000000-0000-4000-8000-000000000088",
        ),
        ("processed_checkpoint_attempt_id", "not-a-uuid"),
        ("processed_receipt_status", "forged"),
        (
            "processed_receipt_calendar_idempotency_key",
            kr_daily_session_idempotency_key(
                provider="toss",
                market="KR",
                session_date=date(2026, 7, 21),
            ),
        ),
        ("processed_receipt_canonical_evidence_sha256", "secret-payload"),
        ("processed_receipt_occurrence_id", "not-a-uuid"),
        ("processed_receipt_revision_inserted", False),
        ("processed_checkpoint_begun_at", datetime(2026, 7, 22, 0, 0)),
        ("limitations", ("secret-payload",)),
        (
            "limitations",
            AlwaysEqualTuple(("secret-limitations-payload",)),
        ),
        (
            "limitations",
            (
                AlwaysEqualText(KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS[0]),
                *KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS[1:],
            ),
        ),
    ],
)
async def test_command_rejects_tampered_success_evidence_with_fixed_failure(
    field_name: str,
    tampered_value: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse_command(_args())
    result = _result()
    object.__setattr__(result, field_name, tampered_value)
    runner = FakeRunner(result)
    runtime = FakeRuntime(runner)

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        return cast(KrCalendarCollectionRuntime, runtime)

    exit_code = await run_command(
        command,
        settings_loader=_settings,
        runtime_factory=runtime_factory,
    )
    output = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert runtime.close_calls == 1
    assert output == (
        "FINAL=FAIL kr_calendar_collection_manual_once production_order_network_requests=0"
    )
    assert "secret" not in output


async def test_command_hides_failure_details_and_closes_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse_command(_args())
    runner = FakeRunner(RuntimeError("secret-token-and-upstream-payload"))
    runtime = FakeRuntime(runner)

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        return cast(KrCalendarCollectionRuntime, runtime)

    exit_code = await run_command(
        command,
        settings_loader=_settings,
        runtime_factory=runtime_factory,
    )
    output = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert runtime.close_calls == 1
    assert output == (
        "FINAL=FAIL kr_calendar_collection_manual_once production_order_network_requests=0"
    )
    assert "secret-token" not in output
    assert "upstream-payload" not in output


async def test_command_propagates_cancellation_after_closing_runtime() -> None:
    command = parse_command(_args())
    runner = FakeRunner(asyncio.CancelledError())
    runtime = FakeRuntime(runner)

    async def runtime_factory(_settings: Settings) -> KrCalendarCollectionRuntime:
        return cast(KrCalendarCollectionRuntime, runtime)

    with pytest.raises(asyncio.CancelledError):
        await run_command(
            command,
            settings_loader=_settings,
            runtime_factory=runtime_factory,
        )

    assert runtime.close_calls == 1
