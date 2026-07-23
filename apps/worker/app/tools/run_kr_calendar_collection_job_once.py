from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

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
from app.config import Settings, load_settings
from app.domain.market_data.point_in_time_calendar import (
    kr_daily_session_idempotency_key,
)
from app.kr_calendar_collection_runtime import (
    KrCalendarCollectionRuntime,
    build_kr_calendar_collection_runtime,
)

ExpectedClassification = Literal["missing", "ready", "paused_retryable"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_FAILURE_LINE = (
    "FINAL=FAIL kr_calendar_collection_manual_once production_order_network_requests=0"
)
_PASS_EVIDENCE_SCHEMA_VERSION = "kr_calendar_collection_manual_run_evidence.v1"


@dataclass(frozen=True, slots=True)
class ManualKrCalendarCollectionCommand:
    spec: KrCalendarCollectionJobSpecV1
    expected_spec_sha256: str
    expected_classification: ExpectedClassification
    expected_revision: int | None
    expected_confirmed_count: int
    expected_next_date: date
    expected_state_reason: str | None
    manual_confirmation_spec_sha256: str
    paused_retry_confirmation_spec_sha256: str | None


def parse_command(
    argv: Sequence[str] | None = None,
) -> ManualKrCalendarCollectionCommand:
    parser = _parser()
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

    expected_spec_sha256 = cast(str, values.expected_spec_sha256)
    manual_confirmation_sha256 = cast(str, values.manual_confirmation_sha256)
    paused_confirmation_sha256 = cast(
        str | None,
        values.paused_confirmation_sha256,
    )
    expected_classification = cast(
        ExpectedClassification,
        values.expected_classification,
    )
    expected_revision = cast(int | None, values.expected_revision)
    expected_confirmed_count = cast(int, values.expected_confirmed_count)
    expected_next_date = cast(date, values.expected_next_date)
    expected_state_reason = cast(str | None, values.expected_state_reason)

    if expected_spec_sha256 != spec.spec_sha256:
        parser.error("expected spec SHA-256 does not match the canonical job spec")
    if manual_confirmation_sha256 != expected_spec_sha256:
        parser.error("manual one-date confirmation does not match the expected spec")
    if expected_confirmed_count >= spec.total_days:
        parser.error("expected confirmed count cannot describe a completed job")
    if expected_next_date != spec.start_date + timedelta(days=expected_confirmed_count):
        parser.error("expected next date does not match the confirmed count")

    if expected_classification == "missing":
        if expected_revision is not None:
            parser.error("missing jobs must omit expected revision")
        if expected_confirmed_count != 0 or expected_next_date != spec.start_date:
            parser.error("missing job precondition is invalid")
        if paused_confirmation_sha256 is not None:
            parser.error("paused retry confirmation is invalid for a missing job")
    else:
        if expected_revision is None:
            parser.error("ready or paused jobs require expected revision")
        if expected_classification == "paused_retryable":
            if expected_state_reason != KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON:
                parser.error("paused retry state reason is not the reviewed pre-write failure")
            if paused_confirmation_sha256 != expected_spec_sha256:
                parser.error("paused retry review does not match the expected spec")
        else:
            if expected_state_reason is not None:
                parser.error("state reason is invalid for a ready job")
            if paused_confirmation_sha256 is not None:
                parser.error("paused retry confirmation is invalid for a ready job")
    if expected_classification == "missing" and expected_state_reason is not None:
        parser.error("state reason is invalid for a missing job")

    return ManualKrCalendarCollectionCommand(
        spec=spec,
        expected_spec_sha256=expected_spec_sha256,
        expected_classification=expected_classification,
        expected_revision=expected_revision,
        expected_confirmed_count=expected_confirmed_count,
        expected_next_date=expected_next_date,
        expected_state_reason=expected_state_reason,
        manual_confirmation_spec_sha256=manual_confirmation_sha256,
        paused_retry_confirmation_spec_sha256=paused_confirmation_sha256,
    )


async def run_command(
    command: ManualKrCalendarCollectionCommand,
    *,
    settings_loader: Callable[[], Settings] = load_settings,
    runtime_factory: Callable[
        [Settings], Awaitable[KrCalendarCollectionRuntime]
    ] = build_kr_calendar_collection_runtime,
) -> int:
    runtime: KrCalendarCollectionRuntime | None = None
    try:
        _validate_command(command)
        settings = settings_loader()
        settings.require_kr_calendar_collection_manual_execution()
        runtime = await runtime_factory(settings)
        try:
            result = await runtime.runner.execute(
                command.spec,
                expected_spec_sha256=command.expected_spec_sha256,
                expected_classification=command.expected_classification,
                expected_revision=command.expected_revision,
                expected_confirmed_count=command.expected_confirmed_count,
                expected_next_date=command.expected_next_date,
                expected_state_reason=command.expected_state_reason,
                manual_confirmation=True,
                paused_retry_confirmation=(
                    command.paused_retry_confirmation_spec_sha256 is not None
                ),
            )
            evidence = _pass_evidence(
                command,
                result,
                expected_holder_id=settings.kr_calendar_collection_holder_id,
            )
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run exactly one guarded durable KR calendar collection date.",
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--start-date", required=True, type=_iso_date)
    parser.add_argument("--end-date", required=True, type=_iso_date)
    parser.add_argument("--expected-spec-sha256", required=True, type=_sha256)
    parser.add_argument(
        "--expected-classification",
        required=True,
        choices=("missing", "ready", "paused_retryable"),
    )
    parser.add_argument("--expected-revision", type=_positive_int)
    parser.add_argument(
        "--expected-confirmed-count",
        required=True,
        type=_nonnegative_int,
    )
    parser.add_argument("--expected-next-date", required=True, type=_iso_date)
    parser.add_argument("--expected-state-reason")
    parser.add_argument(
        "--confirm-one-date-spec-sha256",
        dest="manual_confirmation_sha256",
        required=True,
        type=_sha256,
        metavar="SPEC_SHA256",
    )
    parser.add_argument(
        "--confirm-reviewed-paused-retryable-spec-sha256",
        dest="paused_confirmation_sha256",
        type=_sha256,
        metavar="SPEC_SHA256",
    )
    return parser


def _validate_command(command: object) -> None:
    if type(command) is not ManualKrCalendarCollectionCommand:
        raise ValueError("kr_calendar_collection_manual_command_invalid")
    spec = command.spec
    if type(spec) is not KrCalendarCollectionJobSpecV1:
        raise ValueError("kr_calendar_collection_manual_command_invalid")
    try:
        canonical_spec = KrCalendarCollectionJobSpecV1(
            job_id=spec.job_id,
            provider=spec.provider,
            market=spec.market,
            start_date=spec.start_date,
            end_date=spec.end_date,
            trigger=spec.trigger,
            schema_version=spec.schema_version,
        )
    except Exception:
        raise ValueError("kr_calendar_collection_manual_command_invalid") from None
    if (
        canonical_spec != spec
        or spec.provider != "toss"
        or spec.market != "KR"
        or spec.trigger != "manual"
        or command.expected_spec_sha256 != spec.spec_sha256
        or command.manual_confirmation_spec_sha256 != spec.spec_sha256
        or type(command.expected_confirmed_count) is not int
        or not 0 <= command.expected_confirmed_count < spec.total_days
        or type(command.expected_next_date) is not date
        or command.expected_next_date
        != date.fromordinal(spec.start_date.toordinal() + command.expected_confirmed_count)
        or command.expected_classification not in {"missing", "ready", "paused_retryable"}
    ):
        raise ValueError("kr_calendar_collection_manual_command_invalid")
    if command.expected_classification == "missing":
        valid = (
            command.expected_revision is None
            and command.expected_confirmed_count == 0
            and command.expected_next_date == spec.start_date
            and command.expected_state_reason is None
            and command.paused_retry_confirmation_spec_sha256 is None
        )
    else:
        valid = (
            type(command.expected_revision) is int
            and command.expected_revision > 0
            and (
                (
                    command.expected_classification == "paused_retryable"
                    and command.expected_state_reason
                    == KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
                    and command.paused_retry_confirmation_spec_sha256 == spec.spec_sha256
                )
                or (
                    command.expected_classification == "ready"
                    and command.expected_state_reason is None
                    and command.paused_retry_confirmation_spec_sha256 is None
                )
            )
        )
    if not valid:
        raise ValueError("kr_calendar_collection_manual_command_invalid")


def _pass_evidence(
    command: ManualKrCalendarCollectionCommand,
    result: KrCalendarDateRangeCollectionRunResultV1,
    *,
    expected_holder_id: object,
) -> dict[str, object]:
    if type(result) is not KrCalendarDateRangeCollectionRunResultV1:
        raise ValueError("kr_calendar_collection_manual_result_invalid")
    load_revision = 1 if command.expected_classification == "missing" else command.expected_revision
    if type(load_revision) is not int:
        raise ValueError("kr_calendar_collection_manual_result_invalid")
    expected_confirmed_count = command.expected_confirmed_count + 1
    expected_calendar_idempotency_key = kr_daily_session_idempotency_key(
        provider=command.spec.provider,
        market=command.spec.market,
        session_date=command.expected_next_date,
        schema_version=1,
    )
    expected_completed = expected_confirmed_count == command.spec.total_days
    expected_action = "completed" if expected_completed else "advanced"
    expected_state = "completed" if expected_completed else "ready"
    expected_terminal_manifest = result.terminal_manifest_sha256
    begun_at = result.processed_checkpoint_begun_at
    observed_at = result.processed_receipt_observed_at
    confirmed_at = result.processed_checkpoint_confirmed_at
    receipt_status = result.processed_receipt_status
    if (
        type(result.schema_version) is not str
        or result.schema_version != KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION
        or type(result.job_id) is not str
        or result.job_id != command.spec.job_id
        or type(result.spec_sha256) is not str
        or result.spec_sha256 != command.expected_spec_sha256
        or type(result.provider) is not str
        or result.provider != "toss"
        or type(result.market) is not str
        or result.market != "KR"
        or result.manual_execution_only is not True
        or result.automatic_retry_allowed is not False
        or result.durable_runtime_configured is not True
        or result.full_calendar_certified is not False
        or type(result.limitations) is not tuple
        or any(type(value) is not str for value in result.limitations)
        or result.limitations != KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS
        or type(result.total_date_count) is not int
        or result.total_date_count != command.spec.total_days
        or type(result.confirmed_date_count) is not int
        or result.confirmed_date_count != expected_confirmed_count
        or type(result.remaining_date_count) is not int
        or result.remaining_date_count != result.total_date_count - expected_confirmed_count
        or type(result.action) is not str
        or result.action != expected_action
        or type(result.job_state) is not str
        or result.job_state != expected_state
        or type(result.job_revision) is not int
        or result.job_revision != load_revision + 2
        or type(result.processed_date) is not date
        or result.processed_date != command.expected_next_date
        or (expected_completed and not _is_sha256(expected_terminal_manifest))
        or (not expected_completed and expected_terminal_manifest is not None)
        or not _canonical_uuid4_text(result.processed_checkpoint_attempt_id)
        or not _canonical_uuid4_text(expected_holder_id)
        or not _canonical_uuid4_text(result.processed_checkpoint_holder_id)
        or result.processed_checkpoint_holder_id != expected_holder_id
        or type(result.processed_checkpoint_fencing_revision) is not int
        or result.processed_checkpoint_fencing_revision != load_revision + 1
        or not _ordered_utc_datetimes(begun_at, observed_at, confirmed_at)
        or type(receipt_status) is not str
        or receipt_status not in {"stored", "replayed"}
        or type(result.processed_receipt_calendar_idempotency_key) is not str
        or result.processed_receipt_calendar_idempotency_key != expected_calendar_idempotency_key
        or not _is_sha256(result.processed_receipt_canonical_evidence_sha256)
        or type(result.processed_receipt_revision) is not int
        or result.processed_receipt_revision <= 0
        or type(result.processed_receipt_revision_inserted) is not bool
        or type(result.processed_receipt_occurrence_inserted) is not bool
        or not _canonical_uuid4_text(result.processed_receipt_occurrence_id)
        or (
            receipt_status == "stored"
            and (
                result.processed_receipt_revision_inserted is not True
                or result.processed_receipt_occurrence_inserted is not True
            )
        )
        or (
            receipt_status == "replayed" and result.processed_receipt_revision_inserted is not False
        )
    ):
        raise ValueError("kr_calendar_collection_manual_result_invalid")
    return {
        "action": result.action,
        "automatic_retry_allowed": result.automatic_retry_allowed,
        "confirmed_date_count": result.confirmed_date_count,
        "durable_runtime_configured": result.durable_runtime_configured,
        "expected_classification": command.expected_classification,
        "expected_precondition": {
            "confirmed_date_count": command.expected_confirmed_count,
            "job_revision": command.expected_revision,
            "next_date": command.expected_next_date.isoformat(),
            "state_reason": command.expected_state_reason,
        },
        "final": "PASS",
        "full_calendar_certified": result.full_calendar_certified,
        "job_id": result.job_id,
        "job_revision": result.job_revision,
        "job_state": result.job_state,
        "limitations": list(result.limitations),
        "manual_execution_only": result.manual_execution_only,
        "market": result.market,
        "processed_date": (
            None if result.processed_date is None else result.processed_date.isoformat()
        ),
        "processed_checkpoint_attempt_id": result.processed_checkpoint_attempt_id,
        "processed_checkpoint_begun_at": (
            None
            if result.processed_checkpoint_begun_at is None
            else result.processed_checkpoint_begun_at.isoformat()
        ),
        "processed_checkpoint_confirmed_at": (
            None
            if result.processed_checkpoint_confirmed_at is None
            else result.processed_checkpoint_confirmed_at.isoformat()
        ),
        "processed_checkpoint_fencing_revision": (result.processed_checkpoint_fencing_revision),
        "processed_checkpoint_holder_id": result.processed_checkpoint_holder_id,
        "processed_receipt_calendar_idempotency_key": (
            result.processed_receipt_calendar_idempotency_key
        ),
        "processed_receipt_canonical_evidence_sha256": (
            result.processed_receipt_canonical_evidence_sha256
        ),
        "processed_receipt_observed_at": (
            None
            if result.processed_receipt_observed_at is None
            else result.processed_receipt_observed_at.isoformat()
        ),
        "processed_receipt_occurrence_id": result.processed_receipt_occurrence_id,
        "processed_receipt_occurrence_inserted": (result.processed_receipt_occurrence_inserted),
        "processed_receipt_revision": result.processed_receipt_revision,
        "processed_receipt_revision_inserted": (result.processed_receipt_revision_inserted),
        "processed_receipt_status": result.processed_receipt_status,
        "production_order_network_requests": 0,
        "provider": result.provider,
        "remaining_date_count": result.remaining_date_count,
        "schema_version": _PASS_EVIDENCE_SCHEMA_VERSION,
        "spec_sha256": result.spec_sha256,
        "terminal_manifest_sha256": result.terminal_manifest_sha256,
        "total_date_count": result.total_date_count,
    }


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be an exact YYYY-MM-DD date")
    return parsed


def _sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256")
    return value


def _positive_int(value: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_int(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return int(value)


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _canonical_uuid4_text(value: object) -> bool:
    try:
        parsed = UUID(value) if type(value) is str else None
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed is not None and parsed.version == 4 and str(parsed) == value


def _ordered_utc_datetimes(
    begun_at: object,
    observed_at: object,
    confirmed_at: object,
) -> bool:
    values = (begun_at, observed_at, confirmed_at)
    if not all(
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
        for value in values
    ):
        return False
    return cast(datetime, begun_at) <= cast(datetime, observed_at) <= cast(datetime, confirmed_at)


if __name__ == "__main__":
    raise SystemExit(main())
