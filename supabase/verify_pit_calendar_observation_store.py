#!/usr/bin/env python3
"""Verify independent PIT KR calendar observations in disposable PostgreSQL.

The verifier never connects to a hosted project. It covers fresh and populated
upgrade paths, Python/SQL hash vectors, open and closed session geometry,
revision and occurrence clocks, durable quarantine, concurrency, compatibility
with timing evidence, append-only storage, and the complete ACL boundary.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from app.domain.common.time import KST  # noqa: E402
from app.domain.market_data.daily_candle_timing import (  # noqa: E402
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time_calendar import (  # noqa: E402
    PointInTimeKrDailySessionV1,
)
from verify_g1_g2_migration import (  # noqa: E402
    DB_PASSWORD,
    MIGRATIONS,
    POSTGRES_IMAGE,
    SEED,
    VerificationError,
    apply_repository,
    bootstrap_sql,
    expect_failure,
    jwt_claim_sql,
    psql,
    run,
    wait_for_postgres,
)
from verify_pit_daily_candle_timing_store import (  # noqa: E402
    WORKER_ID,
    append_candle,
    append_timing,
    fixture,
    jsonb_literal,
    request_key,
    scalar,
    sql_text,
    table_count,
)
from verify_pit_daily_candle_timing_store import (  # noqa: E402
    assert_accepted as assert_timing_accepted,
)
from verify_pit_daily_candle_timing_store import (  # noqa: E402
    assert_quarantine as assert_timing_quarantine,
)

MIGRATION_NAME = "20260719040000_pit_calendar_observation_store.sql"
BASE_SESSION_DATE = date(2026, 8, 1)
CALENDAR_CONTRACT_SHA256 = "c" * 64
RECEIPT_FIELDS = {
    "status",
    "calendar_idempotency_key",
    "canonical_evidence_sha256",
    "revision",
    "revision_inserted",
    "occurrence_id",
    "occurrence_inserted",
    "observed_at",
    "quarantine_id",
    "reason_code",
}
CALENDAR_TABLES = (
    "pit_calendar_stream_heads",
    "pit_calendar_content_revisions",
    "pit_calendar_observation_occurrences",
    "pit_calendar_observation_quarantine",
)
APPEND_ONLY_TABLES = (
    "pit_calendar_content_revisions",
    "pit_calendar_observation_occurrences",
    "pit_calendar_observation_quarantine",
)


def calendar_fixture(
    *,
    day_offset: int,
    is_open: bool = True,
    observed_minutes: int = 10,
    regular_end_delta_minutes: int = 0,
    next_end_delta_minutes: int = 0,
) -> PointInTimeKrDailySessionV1:
    session_date = BASE_SESSION_DATE + timedelta(days=day_offset)
    next_date = session_date + timedelta(days=1)
    regular_start = (
        datetime.combine(session_date, time(9, 0), tzinfo=KST)
        if is_open
        else None
    )
    regular_end = (
        datetime.combine(session_date, time(15, 30), tzinfo=KST)
        + timedelta(minutes=regular_end_delta_minutes)
        if is_open
        else None
    )
    next_start = datetime.combine(next_date, time(9, 0), tzinfo=KST)
    next_end = datetime.combine(next_date, time(15, 30), tzinfo=KST) + timedelta(
        minutes=next_end_delta_minutes
    )
    return PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=regular_start,
        regular_end_at=regular_end,
        next_business_date=next_date,
        next_regular_start_at=next_start,
        next_regular_end_at=next_end,
        observed_at=next_start + timedelta(minutes=observed_minutes),
        provider_contract_sha256=CALENDAR_CONTRACT_SHA256,
    )


def reobserve_calendar(
    value: PointInTimeKrDailySessionV1,
    observed_at: datetime,
    *,
    regular_end_delta_minutes: int = 0,
    next_end_delta_minutes: int = 0,
) -> PointInTimeKrDailySessionV1:
    regular_end = value.regular_end_at
    if regular_end is not None:
        regular_end += timedelta(minutes=regular_end_delta_minutes)
    return PointInTimeKrDailySessionV1.create(
        provider=value.provider,
        market=value.market,
        session_date=value.session_date,
        is_open=value.is_open,
        regular_start_at=value.regular_start_at,
        regular_end_at=regular_end,
        next_business_date=value.next_business_date,
        next_regular_start_at=value.next_regular_start_at,
        next_regular_end_at=value.next_regular_end_at
        + timedelta(minutes=next_end_delta_minutes),
        observed_at=observed_at,
        provider_contract_sha256=value.provider_contract_sha256,
    )


def append_calendar_payload(
    container: str,
    payload: dict[str, object],
) -> dict[str, object]:
    rows = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"""
select row_to_json(receipt)
from worker_api.append_pit_kr_daily_session_observation_v1(
  {jsonb_literal(payload, 'independent_calendar')}
) as receipt;
""",
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("PIT calendar RPC returned no receipt")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict) or set(parsed) != RECEIPT_FIELDS:
        raise VerificationError(f"PIT calendar receipt shape mismatch: {parsed}")
    return parsed


def append_calendar(
    container: str,
    value: PointInTimeKrDailySessionV1,
) -> dict[str, object]:
    return append_calendar_payload(container, value.to_payload())


def parsed_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"receipt timestamp is not text: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"receipt timestamp is invalid: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerificationError(f"receipt timestamp lacks timezone: {value!r}")
    return parsed.astimezone(UTC)


def nullable_timestamptz(value: object) -> str:
    if value is None:
        return "null::timestamptz"
    if not isinstance(value, str):
        raise VerificationError(f"timestamp vector is not text: {value!r}")
    return f"{sql_text(value)}::timestamptz"


def calendar_rpc_sql(payload: dict[str, object], tag: str) -> str:
    return f"""
select *
from worker_api.append_pit_kr_daily_session_observation_v1(
  {jsonb_literal(payload, tag)}
);
"""


def assert_accepted(
    receipt: dict[str, object],
    value: PointInTimeKrDailySessionV1,
    *,
    status: str,
    revision: int,
    revision_inserted: bool,
    occurrence_inserted: bool,
    expected_occurrence_id: object | None = None,
) -> str:
    occurrence_id = receipt.get("occurrence_id")
    if (
        receipt.get("status") != status
        or receipt.get("calendar_idempotency_key") != value.idempotency_key
        or receipt.get("canonical_evidence_sha256")
        != value.canonical_evidence_sha256
        or receipt.get("revision") != revision
        or receipt.get("revision_inserted") is not revision_inserted
        or receipt.get("occurrence_inserted") is not occurrence_inserted
        or parsed_timestamp(receipt.get("observed_at"))
        != value.observed_at.astimezone(UTC)
        or receipt.get("quarantine_id") is not None
        or receipt.get("reason_code") is not None
        or not isinstance(occurrence_id, str)
    ):
        raise VerificationError(f"accepted PIT calendar receipt mismatch: {receipt}")
    try:
        UUID(occurrence_id)
    except ValueError as exc:
        raise VerificationError(f"invalid occurrence UUID: {receipt}") from exc
    if expected_occurrence_id is not None and occurrence_id != expected_occurrence_id:
        raise VerificationError(f"PIT calendar occurrence identity changed: {receipt}")
    return occurrence_id


def assert_quarantined(
    receipt: dict[str, object],
    value: PointInTimeKrDailySessionV1,
    reason: str,
    *,
    expected_quarantine_id: object | None = None,
) -> str:
    quarantine_id = receipt.get("quarantine_id")
    if (
        receipt.get("status") != "quarantined"
        or receipt.get("calendar_idempotency_key") != value.idempotency_key
        or receipt.get("canonical_evidence_sha256")
        != value.canonical_evidence_sha256
        or receipt.get("revision_inserted") is not False
        or receipt.get("occurrence_id") is not None
        or receipt.get("occurrence_inserted") is not False
        or parsed_timestamp(receipt.get("observed_at"))
        != value.observed_at.astimezone(UTC)
        or receipt.get("reason_code") != reason
        or not isinstance(quarantine_id, str)
    ):
        raise VerificationError(f"expected PIT calendar quarantine {reason}: {receipt}")
    try:
        UUID(quarantine_id)
    except ValueError as exc:
        raise VerificationError(f"invalid quarantine UUID: {receipt}") from exc
    if expected_quarantine_id is not None and quarantine_id != expected_quarantine_id:
        raise VerificationError(f"PIT calendar quarantine identity changed: {receipt}")
    return quarantine_id


def domain_snapshot(container: str) -> str:
    return scalar(
        container,
        """
select concat_ws('|',
  (select count(*) from public.positions),
  (select count(*) from public.strategy_versions),
  (select count(*) from public.decision_snapshots),
  (select count(*) from public.orders),
  (select count(*) from public.outcomes),
  (select count(*) from public.features_daily),
  (select count(*) from private.order_intents),
  (select count(*) from private.order_attempts));
""",
    )


def verify_python_sql_vectors_and_validation(container: str) -> None:
    for index, value in enumerate(
        (
            calendar_fixture(day_offset=1, is_open=True),
            calendar_fixture(day_offset=2, is_open=False),
        )
    ):
        payload = value.to_payload()
        vector = scalar(
            container,
            f"""
select concat_ws('|',
  private.pit_calendar_identity_sha256_v1(
    {sql_text(value.provider)},
    {sql_text(value.market)},
    {sql_text(value.session_date.isoformat())}::date
  ),
  private.pit_calendar_canonical_evidence_sha256_v1(
    {sql_text(value.provider)},
    {sql_text(value.market)},
    {sql_text(value.session_date.isoformat())}::date,
    {str(value.is_open).lower()}::boolean,
    {nullable_timestamptz(payload['regular_start_at'])},
    {nullable_timestamptz(payload['regular_end_at'])},
    {sql_text(value.next_business_date.isoformat())}::date,
    {sql_text(str(payload['next_regular_start_at']))}::timestamptz,
    {sql_text(str(payload['next_regular_end_at']))}::timestamptz,
    {sql_text(value.provider_contract_sha256)}
  ));
""",
        )
        expected = f"{value.idempotency_key}|{value.canonical_evidence_sha256}"
        if vector != expected:
            raise VerificationError(
                f"Python/SQL calendar vector {index} mismatch: {vector} != {expected}"
            )

    valid = calendar_fixture(day_offset=3)
    bad_hash = dict(valid.to_payload())
    bad_hash["canonical_evidence_sha256"] = "0" * 64
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + calendar_rpc_sql(bad_hash, "bad_hash"),
        "pit_calendar_observation_canonical_evidence_sha256_mismatch",
    )

    extra_key = dict(valid.to_payload())
    extra_key["unexpected"] = True
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + calendar_rpc_sql(extra_key, "extra_key"),
        "pit_calendar_observation_payload_shape_invalid",
    )

    noncanonical = dict(valid.to_payload())
    noncanonical["observed_at"] = str(noncanonical["observed_at"]).replace(
        "Z", "+00:00"
    )
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + calendar_rpc_sql(noncanonical, "noncanonical"),
        "pit_calendar_observation_canonical_value_invalid",
    )

    wrong_kst = dict(valid.to_payload())
    wrong_kst["next_regular_start_at"] = (
        valid.next_regular_start_at - timedelta(days=1)
    ).astimezone(UTC).isoformat().replace("+00:00", "Z")
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + calendar_rpc_sql(wrong_kst, "wrong_kst"),
        "pit_calendar_observation_kst_geometry_invalid",
    )

    closed = calendar_fixture(day_offset=4, is_open=False)
    closed_with_hours = dict(closed.to_payload())
    session_start = datetime.combine(closed.session_date, time(9, 0), tzinfo=KST)
    session_end = datetime.combine(closed.session_date, time(15, 30), tzinfo=KST)
    closed_with_hours["regular_start_at"] = (
        session_start.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    closed_with_hours["regular_end_at"] = (
        session_end.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + calendar_rpc_sql(closed_with_hours, "closed_hours"),
        "pit_calendar_observation_kst_geometry_invalid",
    )
    print("PASS Python/SQL calendar vectors and fail-closed payload validation")


def verify_open_closed_revisions_and_quarantine(container: str) -> None:
    before_domain = domain_snapshot(container)

    open_day = calendar_fixture(day_offset=10)
    initial = append_calendar(container, open_day)
    original_occurrence_id = assert_accepted(
        initial,
        open_day,
        status="stored",
        revision=1,
        revision_inserted=True,
        occurrence_inserted=True,
    )
    assert_accepted(
        append_calendar(container, open_day),
        open_day,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
        expected_occurrence_id=original_occurrence_id,
    )

    later_same = reobserve_calendar(
        open_day,
        open_day.observed_at + timedelta(minutes=1),
    )
    later_receipt = append_calendar(container, later_same)
    later_occurrence_id = assert_accepted(
        later_receipt,
        later_same,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=True,
    )
    if later_occurrence_id == original_occurrence_id:
        raise VerificationError("later unchanged calendar reused occurrence identity")

    assert_accepted(
        append_calendar(container, open_day),
        open_day,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
        expected_occurrence_id=original_occurrence_id,
    )

    correction = reobserve_calendar(
        open_day,
        open_day.observed_at + timedelta(minutes=2),
        regular_end_delta_minutes=1,
    )
    assert_accepted(
        append_calendar(container, correction),
        correction,
        status="stored",
        revision=2,
        revision_inserted=True,
        occurrence_inserted=True,
    )

    before_historical_retry = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_quarantine
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}));
""",
    )
    assert_accepted(
        append_calendar(container, open_day),
        open_day,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
        expected_occurrence_id=original_occurrence_id,
    )
    after_historical_retry = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_quarantine
   where calendar_idempotency_key={sql_text(open_day.idempotency_key)}));
""",
    )
    if after_historical_retry != before_historical_retry:
        raise VerificationError(
            "exact historical retry changed revision, occurrence or quarantine rows"
        )

    recurrence = reobserve_calendar(
        open_day,
        open_day.observed_at + timedelta(minutes=3),
    )
    recurrence_id = assert_quarantined(
        append_calendar(container, recurrence),
        recurrence,
        "pit_calendar_historical_hash_recurrence_ambiguous",
    )
    assert_quarantined(
        append_calendar(container, recurrence),
        recurrence,
        "pit_calendar_historical_hash_recurrence_ambiguous",
        expected_quarantine_id=recurrence_id,
    )

    regression_base = calendar_fixture(day_offset=11, observed_minutes=20)
    append_calendar(container, regression_base)
    regression = reobserve_calendar(
        regression_base,
        regression_base.observed_at - timedelta(minutes=1),
    )
    regression_id = assert_quarantined(
        append_calendar(container, regression),
        regression,
        "pit_calendar_observation_time_regressed",
    )
    assert_quarantined(
        append_calendar(container, regression),
        regression,
        "pit_calendar_observation_time_regressed",
        expected_quarantine_id=regression_id,
    )

    same_clock_base = calendar_fixture(day_offset=12)
    append_calendar(container, same_clock_base)
    same_clock_change = reobserve_calendar(
        same_clock_base,
        same_clock_base.observed_at,
        regular_end_delta_minutes=1,
    )
    conflict_id = assert_quarantined(
        append_calendar(container, same_clock_change),
        same_clock_change,
        "pit_calendar_revision_time_not_increasing",
    )
    assert_quarantined(
        append_calendar(container, same_clock_change),
        same_clock_change,
        "pit_calendar_revision_time_not_increasing",
        expected_quarantine_id=conflict_id,
    )

    closed_day = calendar_fixture(day_offset=13, is_open=False)
    closed_initial = append_calendar(container, closed_day)
    closed_occurrence_id = assert_accepted(
        closed_initial,
        closed_day,
        status="stored",
        revision=1,
        revision_inserted=True,
        occurrence_inserted=True,
    )
    assert_accepted(
        append_calendar(container, closed_day),
        closed_day,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
        expected_occurrence_id=closed_occurrence_id,
    )

    if domain_snapshot(container) != before_domain:
        raise VerificationError("calendar RPC wrote trading/domain tables")

    integrity = scalar(
        container,
        """
select concat_ws('|',
  not exists (
    select 1
    from private.pit_calendar_stream_heads as head
    left join private.pit_calendar_content_revisions as revision
      on revision.calendar_idempotency_key=head.calendar_idempotency_key
     and revision.revision=head.latest_revision
    where revision.id is null
       or revision.canonical_evidence_sha256<>
          head.latest_canonical_evidence_sha256
  ),
  not exists (
    select 1
    from private.pit_calendar_observation_occurrences as occurrence
    join private.pit_calendar_content_revisions as revision
      on revision.id=occurrence.content_revision_id
    where occurrence.calendar_idempotency_key<>
          revision.calendar_idempotency_key
       or occurrence.canonical_evidence_sha256<>
          revision.canonical_evidence_sha256
       or occurrence.observation_payload->>'observed_at'<>
          private.pit_canonical_timestamp_v1(occurrence.observed_at)
  ),
  (select count(*) from private.pit_calendar_observation_quarantine
   where reason_code in (
     'pit_calendar_observation_time_regressed',
     'pit_calendar_revision_time_not_increasing',
     'pit_calendar_historical_hash_recurrence_ambiguous'
   )));
""",
    )
    if integrity != "t|t|3":
        raise VerificationError(
            f"calendar revision/occurrence integrity mismatch: {integrity}"
        )
    print("PASS open/closed storage, replay, correction and durable quarantine")


def verify_concurrency(container: str) -> None:
    exact = calendar_fixture(day_offset=20)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(lambda _: append_calendar(container, exact), range(24))
        )
    if (
        [receipt.get("status") for receipt in receipts].count("stored") != 1
        or [receipt.get("status") for receipt in receipts].count("replayed") != 23
        or sum(receipt.get("revision_inserted") is True for receipt in receipts) != 1
        or sum(receipt.get("occurrence_inserted") is True for receipt in receipts) != 1
        or len({receipt.get("occurrence_id") for receipt in receipts}) != 1
        or table_count(
            container,
            "pit_calendar_content_revisions",
            f"calendar_idempotency_key={sql_text(exact.idempotency_key)}",
        )
        != 1
        or table_count(
            container,
            "pit_calendar_observation_occurrences",
            f"calendar_idempotency_key={sql_text(exact.idempotency_key)}",
        )
        != 1
    ):
        raise VerificationError(
            f"concurrent exact calendar delivery mismatch: {receipts}"
        )

    later = reobserve_calendar(exact, exact.observed_at + timedelta(minutes=1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        later_receipts = list(
            pool.map(lambda _: append_calendar(container, later), range(24))
        )
    if (
        {receipt.get("status") for receipt in later_receipts} != {"replayed"}
        or sum(
            receipt.get("occurrence_inserted") is True
            for receipt in later_receipts
        )
        != 1
        or len({receipt.get("occurrence_id") for receipt in later_receipts}) != 1
        or table_count(
            container,
            "pit_calendar_observation_occurrences",
            f"calendar_idempotency_key={sql_text(exact.idempotency_key)}",
        )
        != 2
    ):
        raise VerificationError(
            f"concurrent later calendar occurrence mismatch: {later_receipts}"
        )

    independent = [calendar_fixture(day_offset=30 + index) for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        independent_receipts = list(
            pool.map(
                lambda value: append_calendar(container, value),
                independent,
            )
        )
    if any(
        receipt.get("status") != "stored"
        or receipt.get("revision_inserted") is not True
        or receipt.get("occurrence_inserted") is not True
        for receipt in independent_receipts
    ):
        raise VerificationError(
            f"concurrent independent calendar streams mismatch: {independent_receipts}"
        )
    print("PASS exact, later-occurrence and independent-stream concurrency")


def verify_cross_rpc_calendar_concurrency(container: str) -> None:
    for index in range(8):
        candle, calendar, timing = fixture(
            day_offset=320 + index,
            symbol=f"25{index:04d}",
        )
        append_candle(container, candle)
        barrier = Barrier(2)

        def append_standalone() -> dict[str, object]:
            barrier.wait()
            return append_calendar(container, calendar)

        def append_with_timing() -> dict[str, object]:
            barrier.wait()
            return append_timing(
                container,
                request_key(f"cross-rpc-calendar-race-{index}"),
                calendar,
                timing,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            standalone_future = pool.submit(append_standalone)
            timing_future = pool.submit(append_with_timing)
            standalone_receipt = standalone_future.result()
            timing_receipt = timing_future.result()

        timing_calendar_inserted = timing_receipt.get("calendar_inserted")
        if not isinstance(timing_calendar_inserted, bool):
            raise VerificationError(
                f"cross-RPC timing receipt lacks calendar flag: {timing_receipt}"
            )
        assert_timing_accepted(
            timing_receipt,
            timing,
            status="stored",
            calendar_revision=1,
            timing_revision=1,
            calendar_inserted=timing_calendar_inserted,
            timing_inserted=True,
        )

        if timing_calendar_inserted:
            assert_accepted(
                standalone_receipt,
                calendar,
                status="replayed",
                revision=1,
                revision_inserted=False,
                occurrence_inserted=False,
            )
        else:
            assert_accepted(
                standalone_receipt,
                calendar,
                status="stored",
                revision=1,
                revision_inserted=True,
                occurrence_inserted=True,
            )

        if (
            int(standalone_receipt.get("revision_inserted") is True)
            + int(timing_calendar_inserted)
            != 1
        ):
            raise VerificationError(
                "cross-RPC race did not elect exactly one calendar writer"
            )

        occurrence_id = standalone_receipt.get("occurrence_id")
        if not isinstance(occurrence_id, str):
            raise VerificationError(
                f"cross-RPC standalone occurrence is invalid: {standalone_receipt}"
            )
        result = scalar(
            container,
            f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_daily_candle_timing_revisions
   where timing_idempotency_key={sql_text(timing.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_quarantine
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_daily_candle_timing_quarantine
   where timing_idempotency_key={sql_text(timing.idempotency_key)}),
  (select count(*)
   from private.pit_daily_candle_timing_revisions as timing_revision
   join private.pit_calendar_observation_occurrences as occurrence
     on occurrence.id=timing_revision.calendar_occurrence_id
   where timing_revision.timing_idempotency_key=
       {sql_text(timing.idempotency_key)}
     and occurrence.id={sql_text(occurrence_id)}::uuid));
""",
        )
        if result != "1|1|1|0|0|1":
            raise VerificationError(
                f"cross-RPC calendar/timing race mismatch: {result}"
            )
    print("PASS standalone and timing RPCs share one calendar stream lock")


def verify_timing_compatibility(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=220, symbol="240001")
    calendar_receipt = append_calendar(container, calendar)
    calendar_occurrence_id = assert_accepted(
        calendar_receipt,
        calendar,
        status="stored",
        revision=1,
        revision_inserted=True,
        occurrence_inserted=True,
    )
    append_candle(container, candle)
    timing_receipt = append_timing(
        container,
        request_key("independent-calendar-timing-compatibility"),
        calendar,
        timing,
    )
    assert_timing_accepted(
        timing_receipt,
        timing,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=False,
        timing_inserted=True,
    )
    binding = scalar(
        container,
        f"""
select count(*)
from private.pit_daily_candle_timing_revisions as timing_revision
join private.pit_calendar_observation_occurrences as occurrence
  on occurrence.id=timing_revision.calendar_occurrence_id
where occurrence.id={sql_text(calendar_occurrence_id)}::uuid
  and occurrence.observation_payload=
      {jsonb_literal(calendar.to_payload(), 'timing_calendar_binding')};
""",
    )
    if binding != "1":
        raise VerificationError(
            "timing evidence did not bind the independent occurrence"
        )

    later_calendar = reobserve_calendar(
        calendar,
        calendar.observed_at + timedelta(minutes=1),
    )
    assert_accepted(
        append_calendar(container, later_calendar),
        later_calendar,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=True,
    )
    timing_revision_count = table_count(
        container,
        "pit_daily_candle_timing_revisions",
        f"timing_idempotency_key={sql_text(timing.idempotency_key)}",
    )
    stale_receipt = append_timing(
        container,
        request_key("independent-calendar-stale-timing-binding"),
        calendar,
        timing,
    )
    assert_timing_quarantine(
        stale_receipt,
        "pit_calendar_observation_time_regressed",
    )
    if (
        stale_receipt.get("calendar_inserted") is not False
        or table_count(
            container,
            "pit_daily_candle_timing_revisions",
            f"timing_idempotency_key={sql_text(timing.idempotency_key)}",
        )
        != timing_revision_count
    ):
        raise VerificationError("stale timing binding wrote a timing revision")
    print(
        "PASS initial timing binding and fail-closed stale-binding limitation"
    )


def verify_catalog_acl_and_append_only(container: str) -> None:
    catalog = scalar(
        container,
        """
select concat_ws('|',
  (select count(*)
   from pg_catalog.pg_proc as procedure
   join pg_catalog.pg_roles as owner_role on owner_role.oid=procedure.proowner
   where procedure.oid in (
     'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure,
     'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure
   ) and owner_role.rolname not in (
     'anon','authenticated','authenticator','service_role'
   )
     and procedure.provolatile='v'
     and procedure.proconfig=array['search_path=""']::text[]),
  (select count(distinct procedure.proowner)=1
   from pg_catalog.pg_proc as procedure
   where procedure.oid in (
     'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure,
     'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure
   )),
  (select procedure.prosecdef
   from pg_catalog.pg_proc as procedure
   where procedure.oid=
     'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)'::regprocedure),
  (select not procedure.prosecdef
   from pg_catalog.pg_proc as procedure
   where procedure.oid=
     'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)'::regprocedure),
  (select count(*)
   from pg_catalog.pg_class as relation
   where relation.oid in (
     'private.pit_calendar_stream_heads'::regclass,
     'private.pit_calendar_content_revisions'::regclass,
     'private.pit_calendar_observation_occurrences'::regclass,
     'private.pit_calendar_observation_quarantine'::regclass
   ) and relation.relrowsecurity),
  (select count(*)=0
   from pg_catalog.pg_policy as policy
   where policy.polrelid in (
     'private.pit_calendar_stream_heads'::regclass,
     'private.pit_calendar_content_revisions'::regclass,
     'private.pit_calendar_observation_occurrences'::regclass,
     'private.pit_calendar_observation_quarantine'::regclass
   )),
  (select count(*)=0
   from information_schema.role_table_grants
   where table_schema='private'
     and table_name in (
       'pit_calendar_stream_heads',
       'pit_calendar_content_revisions',
       'pit_calendar_observation_occurrences',
       'pit_calendar_observation_quarantine'
     ) and grantee in (
       'anon','authenticated','authenticator','service_role'
     )),
  pg_catalog.has_function_privilege(
    'service_role',
    'private.append_pit_kr_daily_session_observation_v1_impl(jsonb)',
    'EXECUTE'
  ),
  pg_catalog.has_function_privilege(
    'service_role',
    'worker_api.append_pit_kr_daily_session_observation_v1(jsonb)',
    'EXECUTE'
  ),
  not pg_catalog.has_function_privilege(
    'service_role',
    'private.put_pit_calendar_observation_occurrence_v1(text,text,uuid,timestamptz,jsonb)',
    'EXECUTE'
  ));
""",
    )
    if catalog != "2|t|t|t|4|t|t|t|t|t":
        raise VerificationError(f"PIT calendar catalog/ACL mismatch: {catalog}")

    sample = calendar_fixture(day_offset=90)
    for role in ("anon", "authenticated"):
        for function_name in (
            "private.append_pit_kr_daily_session_observation_v1_impl",
            "worker_api.append_pit_kr_daily_session_observation_v1",
        ):
            expect_failure(
                container,
                jwt_claim_sql(WORKER_ID, role=role)
                + (
                    f"select * from {function_name}("
                    f"{jsonb_literal(sample.to_payload(), f'acl_{role}')});"
                ),
                "permission denied",
            )

    expect_failure(
        container,
        "set role authenticator; select * from "
        "worker_api.append_pit_kr_daily_session_observation_v1"
        "('{}'::jsonb);",
        "permission denied",
    )

    for role in ("anon", "authenticated", "authenticator", "service_role"):
        for table in CALENDAR_TABLES:
            expect_failure(
                container,
                f"set role {role}; select count(*) from private.{table};",
                "permission denied",
            )

    hostile = calendar_fixture(day_offset=91)
    hostile_receipt = append_calendar(container, hostile)
    assert_accepted(
        hostile_receipt,
        hostile,
        status="stored",
        revision=1,
        revision_inserted=True,
        occurrence_inserted=True,
    )

    for table in APPEND_ONLY_TABLES:
        if table_count(container, table) == 0:
            continue
        expect_failure(
            container,
            f"update private.{table} set id=id where true;",
            "append_only_table_mutation_forbidden",
        )
        expect_failure(
            container,
            f"delete from private.{table} where true;",
            "append_only_table_mutation_forbidden",
        )
    print("PASS owner, ACL, RLS, search_path and append-only contracts")


def apply_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError(
            "PIT calendar observation migration is missing"
        ) from exc

    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    candle, calendar, timing = fixture(day_offset=260, symbol="240002")
    append_candle(container, candle)
    first_timing = append_timing(
        container,
        request_key("calendar-populated-before-target"),
        calendar,
        timing,
    )
    assert_timing_accepted(
        first_timing,
        timing,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )

    later_calendar = reobserve_calendar(
        calendar,
        calendar.observed_at + timedelta(minutes=1),
    )
    later_timing = build_daily_candle_timing_evidence(candle, later_calendar)
    second_timing = append_timing(
        container,
        request_key("calendar-populated-later-occurrence"),
        later_calendar,
        later_timing,
    )
    assert_timing_accepted(
        second_timing,
        later_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )

    public_before = domain_snapshot(container)
    before_counts = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}));
""",
    )
    psql(container, target.read_text(encoding="utf-8"))

    original_replay = append_calendar(container, calendar)
    original_occurrence_id = assert_accepted(
        original_replay,
        calendar,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
    )
    later_replay = append_calendar(container, later_calendar)
    assert_accepted(
        later_replay,
        later_calendar,
        status="replayed",
        revision=1,
        revision_inserted=False,
        occurrence_inserted=False,
    )
    if later_replay.get("occurrence_id") == original_occurrence_id:
        raise VerificationError("populated upgrade collapsed distinct occurrences")

    after_replay_counts = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}));
""",
    )
    if after_replay_counts != before_counts:
        raise VerificationError(
            "populated exact replay changed rows: "
            f"{before_counts} -> {after_replay_counts}"
        )

    correction = reobserve_calendar(
        calendar,
        calendar.observed_at + timedelta(minutes=2),
        regular_end_delta_minutes=1,
    )
    assert_accepted(
        append_calendar(container, correction),
        correction,
        status="stored",
        revision=2,
        revision_inserted=True,
        occurrence_inserted=True,
    )

    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))

    if domain_snapshot(container) != public_before:
        raise VerificationError(
            "populated calendar upgrade changed trading/domain rows"
        )
    verify_catalog_acl_and_append_only(container)
    print("PASS populated upgrade preserves occurrences and accepts later correction")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-calendar-fresh-{suffix}"
    upgrade = f"msp-pit-calendar-upgrade-{suffix}"
    try:
        run(["docker", "info"])
        for container in (fresh, upgrade):
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "-e",
                    f"POSTGRES_PASSWORD={DB_PASSWORD}",
                    POSTGRES_IMAGE,
                ]
            )
            wait_for_postgres(container)

        apply_repository(fresh)
        verify_python_sql_vectors_and_validation(fresh)
        verify_open_closed_revisions_and_quarantine(fresh)
        verify_concurrency(fresh)
        verify_cross_rpc_calendar_concurrency(fresh)
        verify_timing_compatibility(fresh)
        verify_catalog_acl_and_append_only(fresh)
        apply_populated_upgrade(upgrade)
        print("FINAL=PASS pit_calendar_observation_store_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
