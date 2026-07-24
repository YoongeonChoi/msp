#!/usr/bin/env python3
"""Verify append-only PIT source occurrences in disposable PostgreSQL.

The verifier never connects to a hosted project. It checks fresh and populated
upgrade paths, exact source-to-timing bindings, later same-content observations,
component-wise timing clocks, concurrency, durable request receipts, and ACLs.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

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
    append_timing,
    assert_accepted,
    assert_quarantine,
    fixture,
    jsonb_literal,
    request_key,
    scalar,
    sql_text,
    table_count,
)

from app.domain.market_data.daily_candle_timing import (  # noqa: E402
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1  # noqa: E402
from app.domain.market_data.point_in_time_calendar import (  # noqa: E402
    PointInTimeKrDailySessionV1,
)

MIGRATION_NAME = "20260719020000_pit_source_observation_occurrence_store.sql"
OCCURRENCE_TABLES = (
    "pit_candle_observation_occurrences",
    "pit_calendar_observation_occurrences",
)


def reobserve_candle(
    value: PointInTimeCandleV1,
    observed_at: datetime,
    *,
    close_krw: int | None = None,
) -> PointInTimeCandleV1:
    close = value.close_krw if close_krw is None else close_krw
    return PointInTimeCandleV1.create(
        provider=value.provider,
        symbol=value.symbol,
        market=value.market,
        interval=value.interval,
        adjusted=value.adjusted,
        provider_event_at=value.provider_event_at,
        observed_at=observed_at,
        currency=value.currency,
        open_krw=value.open_krw,
        high_krw=max(value.high_krw, close),
        low_krw=value.low_krw,
        close_krw=close,
        volume=value.volume,
        provider_contract_sha256=value.provider_contract_sha256,
    )


def reobserve_calendar(
    value: PointInTimeKrDailySessionV1,
    observed_at: datetime,
    *,
    regular_end_delta_minutes: int = 0,
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
        next_regular_end_at=value.next_regular_end_at,
        observed_at=observed_at,
        provider_contract_sha256=value.provider_contract_sha256,
    )


def append_candle_receipt(
    container: str,
    value: PointInTimeCandleV1,
) -> dict[str, object]:
    rows = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"""
select row_to_json(receipt)
from worker_api.append_pit_candle_observation_v1(
  {jsonb_literal(value.to_payload(), 'candle_occurrence')}
) as receipt;
""",
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("PIT candle occurrence RPC returned no receipt")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict):
        raise VerificationError(f"PIT candle receipt is not an object: {parsed}")
    return parsed


def assert_candle_accepted(
    receipt: dict[str, object],
    value: PointInTimeCandleV1,
    *,
    status: str,
    revision: int,
    inserted: bool,
) -> None:
    if (
        receipt.get("status") != status
        or receipt.get("idempotency_key") != value.idempotency_key
        or receipt.get("canonical_observation_sha256")
        != value.canonical_observation_sha256
        or receipt.get("revision") != revision
        or receipt.get("inserted") is not inserted
        or receipt.get("quarantine_id") is not None
        or receipt.get("reason_code") is not None
    ):
        raise VerificationError(f"accepted candle receipt mismatch: {receipt}")


def assert_candle_quarantine(
    receipt: dict[str, object],
    reason: str,
    *,
    previous_id: object | None = None,
) -> object:
    quarantine_id = receipt.get("quarantine_id")
    if (
        receipt.get("status") != "quarantined"
        or receipt.get("inserted") is not False
        or receipt.get("reason_code") != reason
        or not isinstance(quarantine_id, str)
    ):
        raise VerificationError(f"expected candle quarantine {reason}: {receipt}")
    try:
        UUID(quarantine_id)
    except ValueError as exc:
        raise VerificationError(f"invalid candle quarantine UUID: {receipt}") from exc
    if previous_id is not None and quarantine_id != previous_id:
        raise VerificationError(f"candle quarantine retry changed identity: {receipt}")
    return quarantine_id


def occurrence_count(
    container: str,
    table: str,
    identity_column: str,
    identity: str,
) -> int:
    return table_count(
        container,
        table,
        f"{identity_column}={sql_text(identity)}",
    )


def verify_all_timing_bindings(container: str, *, minimum_rows: int = 1) -> None:
    result = scalar(
        container,
        """
select concat_ws('|',
  (select count(*) from private.pit_daily_candle_timing_revisions),
  (select count(*)
   from private.pit_daily_candle_timing_revisions as timing
   join private.pit_candle_observation_occurrences as candle_occurrence
     on candle_occurrence.id=timing.candle_occurrence_id
   join private.pit_calendar_observation_occurrences as calendar_occurrence
     on calendar_occurrence.id=timing.calendar_occurrence_id
   where candle_occurrence.content_revision_id=timing.candle_revision_id
     and calendar_occurrence.content_revision_id=timing.calendar_revision_id
     and candle_occurrence.idempotency_key=
       timing.timing_payload->>'candle_idempotency_key'
     and calendar_occurrence.calendar_idempotency_key=
       timing.timing_payload->>'calendar_idempotency_key'
     and candle_occurrence.canonical_observation_sha256=
       timing.timing_payload->>'candle_canonical_observation_sha256'
     and calendar_occurrence.canonical_evidence_sha256=
       timing.timing_payload->>'calendar_canonical_evidence_sha256'
     and private.pit_canonical_timestamp_v1(candle_occurrence.observed_at)=
       timing.timing_payload->>'candle_observed_at'
     and private.pit_canonical_timestamp_v1(calendar_occurrence.observed_at)=
       timing.timing_payload->>'calendar_observed_at'
     and candle_occurrence.observation_payload->>'observed_at'=
       timing.timing_payload->>'candle_observed_at'
     and calendar_occurrence.observation_payload->>'observed_at'=
       timing.timing_payload->>'calendar_observed_at')
);
""",
    )
    total_text, exact_text = result.split("|", maxsplit=1)
    total = int(total_text)
    exact = int(exact_text)
    if total < minimum_rows or exact != total:
        raise VerificationError(
            f"timing occurrence binding mismatch: total={total}, exact={exact}"
        )


def verify_catalog_acl_and_append_only(container: str) -> None:
    result = scalar(
        container,
        """
select concat_ws('|',
  (select count(*)
   from pg_catalog.pg_class as rel
   join pg_catalog.pg_namespace as namespace on namespace.oid=rel.relnamespace
   where namespace.nspname='private'
     and rel.relname in (
       'pit_candle_observation_occurrences',
       'pit_calendar_observation_occurrences'
     ) and rel.relkind='r' and rel.relrowsecurity),
  (select count(*)
   from pg_catalog.pg_trigger as trigger_row
   where trigger_row.tgrelid in (
     'private.pit_candle_observation_occurrences'::regclass,
     'private.pit_calendar_observation_occurrences'::regclass
   ) and not trigger_row.tgisinternal),
  (select count(*)=0
   from information_schema.role_table_grants
   where table_schema='private'
     and table_name in (
       'pit_candle_observation_occurrences',
       'pit_calendar_observation_occurrences'
     ) and grantee in ('anon','authenticated','service_role')),
  (select count(*)=2
   from information_schema.columns
   where table_schema='private'
     and table_name='pit_daily_candle_timing_revisions'
     and column_name in ('candle_occurrence_id','calendar_occurrence_id')
     and is_nullable='NO'),
  (select count(*)=2
   from pg_catalog.pg_constraint as constraint_row
   where constraint_row.conrelid=
       'private.pit_daily_candle_timing_revisions'::regclass
     and constraint_row.contype='f'
     and constraint_row.confrelid in (
       'private.pit_candle_observation_occurrences'::regclass,
       'private.pit_calendar_observation_occurrences'::regclass
     ) and constraint_row.convalidated),
  not exists (
    select 1
    from pg_catalog.pg_constraint as constraint_row
    where constraint_row.conrelid=
        'private.pit_daily_candle_timing_revisions'::regclass
      and constraint_row.contype='f'
      and constraint_row.confrelid in (
        'private.pit_candle_observation_occurrences'::regclass,
        'private.pit_calendar_observation_occurrences'::regclass
      ) and not exists (
        select 1
        from pg_catalog.pg_index as index_row
        where index_row.indrelid=constraint_row.conrelid
          and index_row.indisvalid
          and (
            select pg_catalog.array_agg(key.attnum order by key.ordinality)
            from pg_catalog.unnest(index_row.indkey)
              with ordinality as key(attnum, ordinality)
            where key.ordinality <=
              pg_catalog.cardinality(constraint_row.conkey)
          )=constraint_row.conkey
      )
  ),
  (select p.prosecdef and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'private.append_pit_candle_observation_v1_impl(jsonb)'
   )),
  (select p.prosecdef and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
   )),
  (select not p.prosecdef and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'worker_api.append_pit_candle_observation_v1(jsonb)'
   )),
  (select not p.prosecdef and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)'
   ))
);
""",
    )
    if result != "2|2|t|t|t|t|t|t|t|t":
        raise VerificationError(f"PIT occurrence catalog/ACL mismatch: {result}")

    first_columns = {
        "pit_candle_observation_occurrences": "id",
        "pit_calendar_observation_occurrences": "id",
    }
    for role in ("anon", "authenticated", "service_role"):
        for table, first_column in first_columns.items():
            for statement in (
                f"select count(*) from private.{table};",
                f"insert into private.{table} default values;",
                f"update private.{table} set {first_column}={first_column} where false;",
                f"delete from private.{table} where false;",
            ):
                expect_failure(
                    container,
                    f"set role {role}; {statement}",
                    "permission denied",
                )

    for table in OCCURRENCE_TABLES:
        if table_count(container, table) > 0:
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
    print("PASS occurrence RLS, ACL, validated FKs and append-only triggers")


def verify_exact_occurrence_binding(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=50, symbol="150001")
    candle_receipt = append_candle_receipt(container, candle)
    assert_candle_accepted(
        candle_receipt,
        candle,
        status="stored",
        revision=1,
        inserted=True,
    )
    timing_receipt = append_timing(
        container,
        request_key("occurrence-exact-binding"),
        calendar,
        timing,
    )
    assert_accepted(
        timing_receipt,
        timing,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )

    result = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*)
   from private.pit_candle_observation_occurrences as occurrence
   where occurrence.idempotency_key={sql_text(candle.idempotency_key)}
     and occurrence.canonical_observation_sha256=
       {sql_text(candle.canonical_observation_sha256)}
     and occurrence.observation_payload=
       {jsonb_literal(candle.to_payload(), 'exact_candle')}
     and occurrence.record_origin='rpc'),
  (select count(*)
   from private.pit_calendar_observation_occurrences as occurrence
   where occurrence.calendar_idempotency_key=
       {sql_text(calendar.idempotency_key)}
     and occurrence.canonical_evidence_sha256=
       {sql_text(calendar.canonical_evidence_sha256)}
     and occurrence.observation_payload=
       {jsonb_literal(calendar.to_payload(), 'exact_calendar')}
     and occurrence.record_origin='rpc'),
  (select count(*)
   from private.pit_daily_candle_timing_revisions as revision
   where revision.timing_idempotency_key={sql_text(timing.idempotency_key)}
     and revision.candle_occurrence_id is not null
     and revision.calendar_occurrence_id is not null)
);
""",
    )
    if result != "1|1|1":
        raise VerificationError(f"fresh exact occurrence mismatch: {result}")
    verify_all_timing_bindings(container)
    print("PASS fresh sources and timing bind exact immutable occurrences")


def verify_same_content_later_observations(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=51, symbol="150002")
    append_candle_receipt(container, candle)
    first = append_timing(
        container,
        request_key("same-content-base"),
        calendar,
        timing,
    )
    assert_accepted(
        first,
        timing,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )

    later_candle = reobserve_candle(
        candle,
        candle.observed_at + timedelta(minutes=1),
    )
    candle_replay = append_candle_receipt(container, later_candle)
    assert_candle_accepted(
        candle_replay,
        later_candle,
        status="replayed",
        revision=1,
        inserted=False,
    )
    candle_timing = build_daily_candle_timing_evidence(later_candle, calendar)
    second = append_timing(
        container,
        request_key("same-content-later-candle"),
        calendar,
        candle_timing,
    )
    assert_accepted(
        second,
        candle_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )

    later_calendar = reobserve_calendar(
        calendar,
        calendar.observed_at + timedelta(minutes=2),
    )
    later_timing = build_daily_candle_timing_evidence(
        later_candle,
        later_calendar,
    )
    third = append_timing(
        container,
        request_key("same-content-later-calendar"),
        later_calendar,
        later_timing,
    )
    assert_accepted(
        third,
        later_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=3,
        calendar_inserted=False,
        timing_inserted=True,
    )

    replay = append_timing(
        container,
        request_key("same-content-later-calendar-new-request"),
        later_calendar,
        later_timing,
    )
    assert_accepted(
        replay,
        later_timing,
        status="replayed",
        calendar_revision=1,
        timing_revision=3,
        calendar_inserted=False,
        timing_inserted=False,
    )

    counts = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_candle_observation_revisions
   where idempotency_key={sql_text(candle.idempotency_key)}),
  (select count(*) from private.pit_candle_observation_occurrences
   where idempotency_key={sql_text(candle.idempotency_key)}),
  (select count(*) from private.pit_calendar_content_revisions
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences
   where calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_daily_candle_timing_revisions
   where timing_idempotency_key={sql_text(timing.idempotency_key)})
);
""",
    )
    if counts != "1|2|1|2|3":
        raise VerificationError(f"same-content occurrence counts mismatch: {counts}")
    verify_all_timing_bindings(container, minimum_rows=4)
    print("PASS later candle and calendar observations preserve each occurrence")


def verify_same_availability_component_monotonicity(container: str) -> None:
    candle_a, calendar_a, timing_a = fixture(
        day_offset=52,
        symbol="150003",
        calendar_observed_minutes=3,
    )
    append_candle_receipt(container, candle_a)
    append_timing(
        container,
        request_key("same-available-candle-base"),
        calendar_a,
        timing_a,
    )
    later_candle = reobserve_candle(
        candle_a,
        candle_a.observed_at + timedelta(minutes=1),
    )
    append_candle_receipt(container, later_candle)
    later_candle_timing = build_daily_candle_timing_evidence(
        later_candle,
        calendar_a,
    )
    if later_candle_timing.evidence_available_at != timing_a.evidence_available_at:
        raise VerificationError("candle component fixture changed availability clock")
    candle_component = append_timing(
        container,
        request_key("same-available-candle-later"),
        calendar_a,
        later_candle_timing,
    )
    assert_accepted(
        candle_component,
        later_candle_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )

    candle_b, calendar_b, timing_b = fixture(
        day_offset=53,
        symbol="150004",
        candle_observed_minutes=3,
    )
    append_candle_receipt(container, candle_b)
    append_timing(
        container,
        request_key("same-available-calendar-base"),
        calendar_b,
        timing_b,
    )
    later_calendar = reobserve_calendar(
        calendar_b,
        calendar_b.observed_at + timedelta(minutes=1),
    )
    later_calendar_timing = build_daily_candle_timing_evidence(
        candle_b,
        later_calendar,
    )
    if later_calendar_timing.evidence_available_at != timing_b.evidence_available_at:
        raise VerificationError("calendar component fixture changed availability clock")
    calendar_component = append_timing(
        container,
        request_key("same-available-calendar-later"),
        later_calendar,
        later_calendar_timing,
    )
    assert_accepted(
        calendar_component,
        later_calendar_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )
    verify_all_timing_bindings(container, minimum_rows=8)
    print("PASS either source component may advance at the same max availability")


def verify_regression_and_aba_guards(container: str) -> None:
    candle_r, _, _ = fixture(
        day_offset=54,
        symbol="150005",
        candle_observed_minutes=2,
    )
    append_candle_receipt(container, candle_r)
    stale_candle = reobserve_candle(
        candle_r,
        candle_r.observed_at - timedelta(minutes=1),
        close_krw=72_100,
    )
    stale_candle_receipt = append_candle_receipt(container, stale_candle)
    stale_candle_id = assert_candle_quarantine(
        stale_candle_receipt,
        "candle_observation_store_observation_time_regressed",
    )
    assert_candle_quarantine(
        append_candle_receipt(container, stale_candle),
        "candle_observation_store_observation_time_regressed",
        previous_id=stale_candle_id,
    )

    candle_a, _, _ = fixture(day_offset=55, symbol="150006")
    candle_b = reobserve_candle(
        candle_a,
        candle_a.observed_at + timedelta(minutes=1),
        close_krw=72_100,
    )
    candle_a_again = reobserve_candle(
        candle_a,
        candle_a.observed_at + timedelta(minutes=2),
    )
    append_candle_receipt(container, candle_a)
    append_candle_receipt(container, candle_b)
    assert_candle_quarantine(
        append_candle_receipt(container, candle_a_again),
        "candle_observation_store_historical_hash_recurrence_ambiguous",
    )

    candle_cr, calendar_cr, timing_cr = fixture(
        day_offset=56,
        symbol="150007",
        calendar_observed_minutes=2,
    )
    append_candle_receipt(container, candle_cr)
    append_timing(
        container,
        request_key("occurrence-calendar-regression-base"),
        calendar_cr,
        timing_cr,
    )
    stale_calendar = reobserve_calendar(
        calendar_cr,
        calendar_cr.observed_at - timedelta(minutes=1),
        regular_end_delta_minutes=-5,
    )
    stale_timing = build_daily_candle_timing_evidence(
        candle_cr,
        stale_calendar,
    )
    stale_calendar_receipt = append_timing(
        container,
        request_key("occurrence-calendar-regression-candidate"),
        stale_calendar,
        stale_timing,
    )
    assert_quarantine(
        stale_calendar_receipt,
        "pit_calendar_observation_time_regressed",
    )

    candle_ca, calendar_a, timing_a = fixture(
        day_offset=57,
        symbol="150008",
    )
    append_candle_receipt(container, candle_ca)
    append_timing(
        container,
        request_key("occurrence-calendar-aba-a"),
        calendar_a,
        timing_a,
    )
    calendar_b = reobserve_calendar(
        calendar_a,
        calendar_a.observed_at + timedelta(minutes=1),
        regular_end_delta_minutes=-5,
    )
    timing_b = build_daily_candle_timing_evidence(candle_ca, calendar_b)
    append_timing(
        container,
        request_key("occurrence-calendar-aba-b"),
        calendar_b,
        timing_b,
    )
    calendar_a_again = reobserve_calendar(
        calendar_a,
        calendar_a.observed_at + timedelta(minutes=2),
    )
    timing_a_again = build_daily_candle_timing_evidence(
        candle_ca,
        calendar_a_again,
    )
    assert_quarantine(
        append_timing(
            container,
            request_key("occurrence-calendar-aba-a-again"),
            calendar_a_again,
            timing_a_again,
        ),
        "pit_calendar_historical_hash_recurrence_ambiguous",
    )

    timing_old_candle, timing_calendar, timing_old = fixture(
        day_offset=58,
        symbol="150009",
    )
    timing_new_candle = reobserve_candle(
        timing_old_candle,
        timing_old_candle.observed_at + timedelta(minutes=2),
        close_krw=72_100,
    )
    timing_new = build_daily_candle_timing_evidence(
        timing_new_candle,
        timing_calendar,
    )
    append_candle_receipt(container, timing_old_candle)
    append_candle_receipt(container, timing_new_candle)
    append_timing(
        container,
        request_key("occurrence-timing-regression-new"),
        timing_calendar,
        timing_new,
    )
    assert_quarantine(
        append_timing(
            container,
            request_key("occurrence-timing-regression-old"),
            timing_calendar,
            timing_old,
        ),
        "pit_timing_observation_time_regressed",
    )

    timing_a_candle, timing_a_calendar, timing_a_value = fixture(
        day_offset=59,
        symbol="150010",
    )
    timing_b_candle = reobserve_candle(
        timing_a_candle,
        timing_a_candle.observed_at + timedelta(minutes=1),
        close_krw=72_100,
    )
    timing_b_value = build_daily_candle_timing_evidence(
        timing_b_candle,
        timing_a_calendar,
    )
    append_candle_receipt(container, timing_a_candle)
    append_timing(
        container,
        request_key("occurrence-timing-aba-a"),
        timing_a_calendar,
        timing_a_value,
    )
    append_candle_receipt(container, timing_b_candle)
    append_timing(
        container,
        request_key("occurrence-timing-aba-b"),
        timing_a_calendar,
        timing_b_value,
    )
    assert_quarantine(
        append_timing(
            container,
            request_key("occurrence-timing-aba-a-again"),
            timing_a_calendar,
            timing_a_value,
        ),
        "pit_timing_historical_hash_recurrence_ambiguous",
    )
    verify_all_timing_bindings(container, minimum_rows=13)
    print("PASS source and timing regression plus A-B-A guards remain fail-closed")


def verify_concurrent_delivery(container: str) -> None:
    candle, _, _ = fixture(day_offset=60, symbol="150011")
    with ThreadPoolExecutor(max_workers=16) as pool:
        candle_receipts = list(
            pool.map(
                lambda _index: append_candle_receipt(container, candle),
                range(32),
            )
        )
    candle_statuses = [receipt.get("status") for receipt in candle_receipts]
    if (
        candle_statuses.count("stored") != 1
        or candle_statuses.count("replayed") != 31
        or occurrence_count(
            container,
            "pit_candle_observation_occurrences",
            "idempotency_key",
            candle.idempotency_key,
        )
        != 1
        or table_count(
            container,
            "pit_candle_observation_revisions",
            f"idempotency_key={sql_text(candle.idempotency_key)}",
        )
        != 1
    ):
        raise VerificationError(
            f"32-way candle occurrence delivery mismatch: {candle_receipts}"
        )

    timing_candle, timing_calendar, timing = fixture(
        day_offset=61,
        symbol="150012",
    )
    append_candle_receipt(container, timing_candle)
    timing_key = request_key("occurrence-concurrent-exact")
    with ThreadPoolExecutor(max_workers=16) as pool:
        timing_receipts = list(
            pool.map(
                lambda _index: append_timing(
                    container,
                    timing_key,
                    timing_calendar,
                    timing,
                ),
                range(32),
            )
        )
    if (
        any(receipt != timing_receipts[0] for receipt in timing_receipts[1:])
        or {receipt.get("status") for receipt in timing_receipts} != {"stored"}
        or occurrence_count(
            container,
            "pit_calendar_observation_occurrences",
            "calendar_idempotency_key",
            timing_calendar.idempotency_key,
        )
        != 1
        or table_count(
            container,
            "pit_daily_candle_timing_revisions",
            f"timing_idempotency_key={sql_text(timing.idempotency_key)}",
        )
        != 1
    ):
        raise VerificationError(
            f"32-way exact timing occurrence mismatch: {timing_receipts}"
        )

    independent_candle, independent_calendar, independent_timing = fixture(
        day_offset=62,
        symbol="150013",
    )
    append_candle_receipt(container, independent_candle)
    independent_keys = [
        request_key(f"occurrence-concurrent-independent-{index}")
        for index in range(16)
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        independent_receipts = list(
            pool.map(
                lambda key: append_timing(
                    container,
                    key,
                    independent_calendar,
                    independent_timing,
                ),
                independent_keys,
            )
        )
    statuses = [receipt.get("status") for receipt in independent_receipts]
    if (
        statuses.count("stored") != 1
        or statuses.count("replayed") != 15
        or [
            receipt.get("calendar_inserted") for receipt in independent_receipts
        ].count(True)
        != 1
        or [
            receipt.get("timing_inserted") for receipt in independent_receipts
        ].count(True)
        != 1
        or occurrence_count(
            container,
            "pit_calendar_observation_occurrences",
            "calendar_idempotency_key",
            independent_calendar.idempotency_key,
        )
        != 1
        or table_count(
            container,
            "pit_daily_candle_timing_revisions",
            (
                "timing_idempotency_key="
                f"{sql_text(independent_timing.idempotency_key)}"
            ),
        )
        != 1
    ):
        raise VerificationError(
            "16-way independent request occurrence mismatch: "
            f"{independent_receipts}"
        )
    verify_all_timing_bindings(container, minimum_rows=16)
    print("PASS 32-way exact and 16-way independent occurrence delivery")


def verify_request_replay(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=63, symbol="150014")
    append_candle_receipt(container, candle)
    key = request_key("occurrence-request-replay")
    first = append_timing(container, key, calendar, timing)
    replay = append_timing(container, key, calendar, timing)
    if first != replay or first.get("status") != "stored":
        raise VerificationError(f"request replay changed receipt: {first} / {replay}")

    changed_candle = reobserve_candle(
        candle,
        candle.observed_at + timedelta(minutes=1),
        close_krw=72_100,
    )
    append_candle_receipt(container, changed_candle)
    changed_timing = build_daily_candle_timing_evidence(
        changed_candle,
        calendar,
    )
    conflict = append_timing(container, key, calendar, changed_timing)
    conflict_id = assert_quarantine(
        conflict,
        "pit_timing_request_idempotency_conflict",
    )
    conflict_replay = append_timing(container, key, calendar, changed_timing)
    assert_quarantine(
        conflict_replay,
        "pit_timing_request_idempotency_conflict",
        previous_id=conflict_id,
    )
    if conflict != conflict_replay:
        raise VerificationError(
            f"request conflict receipt changed: {conflict} / {conflict_replay}"
        )

    counts = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_daily_candle_timing_request_ledger
   where request_idempotency_key={sql_text(key)}),
  (select count(*) from private.pit_daily_candle_timing_request_receipts
   where request_idempotency_key={sql_text(key)}),
  (select count(*) from private.pit_daily_candle_timing_revisions
   where timing_idempotency_key={sql_text(timing.idempotency_key)})
);
""",
    )
    if counts != "1|2|1":
        raise VerificationError(f"request replay durability mismatch: {counts}")
    print("PASS exact request replay and conflicting request quarantine are stable")


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("PIT source occurrence migration is missing") from exc

    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    settings_before = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    )
    if not settings_before:
        raise VerificationError("populated occurrence upgrade lacks bot settings")

    backfill_candle, backfill_calendar, backfill_timing = fixture(
        day_offset=70,
        symbol="160001",
    )
    append_candle_receipt(container, backfill_candle)
    append_timing(
        container,
        request_key("occurrence-upgrade-exact-backfill"),
        backfill_calendar,
        backfill_timing,
    )

    recovery_candle, recovery_calendar, recovery_timing = fixture(
        day_offset=71,
        symbol="160002",
    )
    append_candle_receipt(container, recovery_candle)
    append_timing(
        container,
        request_key("occurrence-upgrade-recovery-base"),
        recovery_calendar,
        recovery_timing,
    )
    later_recovery_candle = reobserve_candle(
        recovery_candle,
        recovery_candle.observed_at + timedelta(minutes=5),
    )
    legacy_replay = append_candle_receipt(container, later_recovery_candle)
    assert_candle_accepted(
        legacy_replay,
        later_recovery_candle,
        status="replayed",
        revision=1,
        inserted=False,
    )
    later_recovery_timing = build_daily_candle_timing_evidence(
        later_recovery_candle,
        recovery_calendar,
    )
    legacy_key = request_key("occurrence-upgrade-legacy-quarantine")
    legacy_quarantine = append_timing(
        container,
        legacy_key,
        recovery_calendar,
        later_recovery_timing,
    )
    legacy_quarantine_id = assert_quarantine(
        legacy_quarantine,
        "pit_timing_candle_revision_missing",
    )

    calendar_candle, calendar_base, calendar_timing = fixture(
        day_offset=72,
        symbol="160003",
    )
    append_candle_receipt(container, calendar_candle)
    append_timing(
        container,
        request_key("occurrence-upgrade-calendar-head-base"),
        calendar_base,
        calendar_timing,
    )
    recovered_calendar = reobserve_calendar(
        calendar_base,
        calendar_base.observed_at + timedelta(minutes=5),
    )
    psql(
        container,
        f"""
update private.pit_calendar_stream_heads
set last_seen_observed_at={sql_text(recovered_calendar.to_payload()['observed_at'])}
      ::timestamptz,
    updated_at=clock_timestamp()
where calendar_idempotency_key={sql_text(calendar_base.idempotency_key)};
""",
    )

    pre_counts = scalar(
        container,
        """
select concat_ws('|',
  (select count(*) from private.pit_candle_observation_revisions),
  (select count(*) from private.pit_calendar_content_revisions),
  (select count(*) from private.pit_daily_candle_timing_revisions)
);
""",
    )
    psql(container, target.read_text(encoding="utf-8"))

    backfill_counts = scalar(
        container,
        """
select concat_ws('|',
  (select count(*) from private.pit_candle_observation_revisions),
  (select count(*) from private.pit_calendar_content_revisions),
  (select count(*) from private.pit_daily_candle_timing_revisions),
  (select count(*)
   from private.pit_candle_observation_revisions as revision
   join private.pit_candle_observation_occurrences as occurrence
     on occurrence.content_revision_id=revision.id
    and occurrence.idempotency_key=revision.idempotency_key
    and occurrence.canonical_observation_sha256=
      revision.canonical_observation_sha256
    and occurrence.observed_at=revision.observed_at
    and occurrence.observation_payload=revision.candle_payload
    and occurrence.record_origin='content_revision_backfill'),
  (select count(*)
   from private.pit_calendar_content_revisions as revision
   join private.pit_calendar_observation_occurrences as occurrence
     on occurrence.content_revision_id=revision.id
    and occurrence.calendar_idempotency_key=
      revision.calendar_idempotency_key
    and occurrence.canonical_evidence_sha256=
      revision.canonical_evidence_sha256
    and occurrence.observed_at=revision.observed_at
    and occurrence.observation_payload=revision.calendar_payload
    and occurrence.record_origin='content_revision_backfill')
);
""",
    )
    candle_revisions, calendar_revisions, timing_revisions = pre_counts.split("|")
    expected_backfill = "|".join(
        (
            candle_revisions,
            calendar_revisions,
            timing_revisions,
            candle_revisions,
            calendar_revisions,
        )
    )
    if backfill_counts != expected_backfill:
        raise VerificationError(
            f"content revision occurrence backfill mismatch: {backfill_counts}"
        )

    recovery_counts = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*)
   from private.pit_candle_observation_occurrences as occurrence
   where occurrence.idempotency_key={sql_text(recovery_candle.idempotency_key)}
     and occurrence.observed_at=
       {sql_text(later_recovery_candle.to_payload()['observed_at'])}::timestamptz
     and occurrence.observation_payload=
       {jsonb_literal(later_recovery_candle.to_payload(), 'recovered_candle')}
     and occurrence.record_origin='stream_head_recovery'),
  (select count(*)
   from private.pit_calendar_observation_occurrences as occurrence
   where occurrence.calendar_idempotency_key=
       {sql_text(calendar_base.idempotency_key)}
     and occurrence.observed_at=
       {sql_text(recovered_calendar.to_payload()['observed_at'])}::timestamptz
     and occurrence.observation_payload=
       {jsonb_literal(recovered_calendar.to_payload(), 'recovered_calendar')}
     and occurrence.record_origin='stream_head_recovery')
);
""",
    )
    if recovery_counts != "1|1":
        raise VerificationError(f"stream head occurrence recovery mismatch: {recovery_counts}")

    verify_catalog_acl_and_append_only(container)
    verify_all_timing_bindings(container, minimum_rows=int(timing_revisions))

    legacy_retry = append_timing(
        container,
        legacy_key,
        recovery_calendar,
        later_recovery_timing,
    )
    assert_quarantine(
        legacy_retry,
        "pit_timing_candle_revision_missing",
        previous_id=legacy_quarantine_id,
    )
    if legacy_retry != legacy_quarantine:
        raise VerificationError(
            "migration changed the original quarantined request receipt: "
            f"{legacy_quarantine} / {legacy_retry}"
        )

    recovered_timing_receipt = append_timing(
        container,
        request_key("occurrence-upgrade-recovered-new-request"),
        recovery_calendar,
        later_recovery_timing,
    )
    assert_accepted(
        recovered_timing_receipt,
        later_recovery_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )

    recovered_calendar_timing = build_daily_candle_timing_evidence(
        calendar_candle,
        recovered_calendar,
    )
    recovered_calendar_receipt = append_timing(
        container,
        request_key("occurrence-upgrade-calendar-recovered-new-request"),
        recovered_calendar,
        recovered_calendar_timing,
    )
    assert_accepted(
        recovered_calendar_receipt,
        recovered_calendar_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )
    verify_all_timing_bindings(container, minimum_rows=int(timing_revisions) + 2)

    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))

    settings_after = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    )
    if settings_after != settings_before:
        raise VerificationError("occurrence upgrade changed populated bot settings")

    verify_catalog_acl_and_append_only(container)
    verify_all_timing_bindings(container, minimum_rows=int(timing_revisions) + 2)
    future_candle, future_calendar, future_timing = fixture(
        day_offset=73,
        symbol="160004",
    )
    append_candle_receipt(container, future_candle)
    future_receipt = append_timing(
        container,
        request_key("occurrence-upgrade-after-future-migrations"),
        future_calendar,
        future_timing,
    )
    assert_accepted(
        future_receipt,
        future_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )
    verify_all_timing_bindings(container, minimum_rows=int(timing_revisions) + 3)
    print("PASS populated backfill, head recovery and future migration convergence")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-occurrence-fresh-{suffix}"
    upgrade = f"msp-pit-occurrence-upgrade-{suffix}"
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
        verify_catalog_acl_and_append_only(fresh)
        verify_exact_occurrence_binding(fresh)
        verify_same_content_later_observations(fresh)
        verify_same_availability_component_monotonicity(fresh)
        verify_regression_and_aba_guards(fresh)
        verify_concurrent_delivery(fresh)
        verify_request_replay(fresh)
        verify_populated_upgrade(upgrade)
        print("FINAL=PASS pit_source_observation_occurrence_store_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
