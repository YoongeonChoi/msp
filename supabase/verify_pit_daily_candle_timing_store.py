#!/usr/bin/env python3
"""Verify the durable PIT daily-candle timing store in disposable PostgreSQL.

The verifier never connects to a hosted project.  It exercises the exact
migration boundary on both a fresh repository and a populated pre-migration
database, then proves canonical hashes, source binding, concurrency,
idempotency, revision clocks, durable quarantine, and the private ACL surface.
"""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
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

from app.domain.common.time import KST  # noqa: E402
from app.domain.market_data.daily_candle_timing import (  # noqa: E402
    PointInTimeDailyCandleTimingEvidenceV1,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1  # noqa: E402
from app.domain.market_data.point_in_time_calendar import (  # noqa: E402
    PointInTimeKrDailySessionV1,
)

MIGRATION_NAME = "20260719010000_pit_daily_candle_timing_store.sql"
WORKER_ID = "73737373-7373-4373-8373-737373737373"
CANDLE_CONTRACT_SHA256 = "a" * 64
CALENDAR_CONTRACT_SHA256 = "b" * 64
BASE_SESSION_DATE = date(2026, 4, 1)

PRIVATE_TABLES = (
    "pit_calendar_stream_heads",
    "pit_calendar_content_revisions",
    "pit_calendar_observation_quarantine",
    "pit_daily_candle_timing_heads",
    "pit_daily_candle_timing_revisions",
    "pit_daily_candle_timing_quarantine",
    "pit_daily_candle_timing_request_ledger",
    "pit_daily_candle_timing_request_receipts",
)
APPEND_ONLY_TABLES = (
    "pit_calendar_content_revisions",
    "pit_calendar_observation_quarantine",
    "pit_daily_candle_timing_revisions",
    "pit_daily_candle_timing_quarantine",
    "pit_daily_candle_timing_request_ledger",
    "pit_daily_candle_timing_request_receipts",
)
RECEIPT_FIELDS = {
    "status",
    "request_idempotency_key",
    "timing_idempotency_key",
    "canonical_timing_evidence_sha256",
    "calendar_revision",
    "timing_revision",
    "calendar_inserted",
    "timing_inserted",
    "evidence_available_at",
    "quarantine_id",
    "reason_code",
}


def request_key(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def fixture(
    *,
    day_offset: int,
    symbol: str,
    close_krw: int = 72_000,
    candle_observed_minutes: int = 0,
    calendar_observed_minutes: int = 0,
    regular_end_delta_minutes: int = 0,
    timestamp_microseconds: int = 0,
) -> tuple[
    PointInTimeCandleV1,
    PointInTimeKrDailySessionV1,
    PointInTimeDailyCandleTimingEvidenceV1,
]:
    session_date = BASE_SESSION_DATE + timedelta(days=day_offset)
    next_date = session_date + timedelta(days=1)
    microseconds = timedelta(microseconds=timestamp_microseconds)
    candle_event = (
        datetime.combine(session_date, time(0, 0), tzinfo=KST) + microseconds
    )
    regular_start = (
        datetime.combine(session_date, time(9, 0), tzinfo=KST) + microseconds
    )
    regular_end = (
        datetime.combine(session_date, time(15, 30), tzinfo=KST)
        + timedelta(minutes=regular_end_delta_minutes)
        + microseconds
    )
    cutoff = datetime.combine(next_date, time(9, 0), tzinfo=KST) + microseconds
    next_end = datetime.combine(next_date, time(15, 30), tzinfo=KST) + microseconds
    candle_observed_at = cutoff + timedelta(minutes=candle_observed_minutes)
    calendar_observed_at = cutoff + timedelta(minutes=calendar_observed_minutes)

    candle = PointInTimeCandleV1.create(
        provider="toss",
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=candle_event,
        observed_at=candle_observed_at,
        currency="KRW",
        open_krw=71_600,
        high_krw=max(72_300, close_krw),
        low_krw=71_500,
        close_krw=close_krw,
        volume=3_521_000,
        provider_contract_sha256=CANDLE_CONTRACT_SHA256,
    )
    calendar = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=True,
        regular_start_at=regular_start,
        regular_end_at=regular_end,
        next_business_date=next_date,
        next_regular_start_at=cutoff,
        next_regular_end_at=next_end,
        observed_at=calendar_observed_at,
        provider_contract_sha256=CALENDAR_CONTRACT_SHA256,
    )
    timing = build_daily_candle_timing_evidence(candle, calendar)
    return candle, calendar, timing


def jsonb_literal(payload: dict[str, object], tag: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    delimiter = f"${tag}$"
    if delimiter in raw:
        raise VerificationError(f"unexpected {tag} payload delimiter")
    return f"{delimiter}{raw}{delimiter}::jsonb"


def sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def append_candle(container: str, value: PointInTimeCandleV1) -> dict[str, object]:
    rows = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"""
select row_to_json(receipt)
from worker_api.append_pit_candle_observation_v1(
  {jsonb_literal(value.to_payload(), 'candle')}
) as receipt;
""",
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("PIT candle source RPC returned no receipt")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict) or parsed.get("status") not in {"stored", "replayed"}:
        raise VerificationError(f"PIT candle source was not accepted: {parsed}")
    return parsed


def append_payloads(
    container: str,
    key: str,
    calendar_payload: dict[str, object],
    timing_payload: dict[str, object],
) -> dict[str, object]:
    rows = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"""
select row_to_json(receipt)
from worker_api.append_pit_daily_candle_timing_evidence_v1(
  {sql_text(key)},
  {jsonb_literal(calendar_payload, 'calendar')},
  {jsonb_literal(timing_payload, 'timing')}
) as receipt;
""",
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("PIT timing RPC returned no receipt")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict) or set(parsed) != RECEIPT_FIELDS:
        raise VerificationError(f"PIT timing receipt shape mismatch: {parsed}")
    if parsed.get("request_idempotency_key") != key:
        raise VerificationError(f"PIT timing request key mismatch: {parsed}")
    return parsed


def append_timing(
    container: str,
    key: str,
    calendar: PointInTimeKrDailySessionV1,
    timing: PointInTimeDailyCandleTimingEvidenceV1,
) -> dict[str, object]:
    return append_payloads(container, key, calendar.to_payload(), timing.to_payload())


def scalar(container: str, query: str) -> str:
    return psql(container, query).stdout.strip()


def table_count(container: str, table: str, predicate: str = "true") -> int:
    return int(scalar(container, f"select count(*) from private.{table} where {predicate};"))


def assert_quarantine(
    receipt: dict[str, object],
    reason: str,
    *,
    previous_id: object | None = None,
) -> object:
    quarantine_id = receipt.get("quarantine_id")
    if (
        receipt.get("status") != "quarantined"
        or receipt.get("reason_code") != reason
        or receipt.get("timing_inserted") is not False
        or not isinstance(quarantine_id, str)
    ):
        raise VerificationError(f"expected quarantine {reason}: {receipt}")
    try:
        UUID(quarantine_id)
    except ValueError as exc:
        raise VerificationError(f"invalid quarantine UUID: {receipt}") from exc
    if previous_id is not None and quarantine_id != previous_id:
        raise VerificationError(f"quarantine retry changed identity: {receipt}")
    return quarantine_id


def assert_accepted(
    receipt: dict[str, object],
    timing: PointInTimeDailyCandleTimingEvidenceV1,
    *,
    status: str,
    calendar_revision: int,
    timing_revision: int,
    calendar_inserted: bool,
    timing_inserted: bool,
) -> None:
    expected_available = timing.to_payload()["evidence_available_at"]
    actual_available = receipt.get("evidence_available_at")
    if isinstance(actual_available, str):
        actual_available = (
            datetime.fromisoformat(actual_available.replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if (
        receipt.get("status") != status
        or receipt.get("timing_idempotency_key") != timing.idempotency_key
        or receipt.get("canonical_timing_evidence_sha256")
        != timing.canonical_timing_evidence_sha256
        or receipt.get("calendar_revision") != calendar_revision
        or receipt.get("timing_revision") != timing_revision
        or receipt.get("calendar_inserted") is not calendar_inserted
        or receipt.get("timing_inserted") is not timing_inserted
        or actual_available != expected_available
        or receipt.get("quarantine_id") is not None
        or receipt.get("reason_code") is not None
    ):
        raise VerificationError(f"accepted PIT timing receipt mismatch: {receipt}")


def verify_python_sql_golden_vectors(container: str) -> None:
    vectors = (
        fixture(day_offset=20, symbol="120001"),
        fixture(
            day_offset=21,
            symbol="120002",
            candle_observed_minutes=1,
            calendar_observed_minutes=2,
            regular_end_delta_minutes=-7,
            timestamp_microseconds=123456,
        ),
    )
    for _candle, calendar, timing in vectors:
        calendar_payload = calendar.to_payload()
        timing_payload = timing.to_payload()
        if calendar.regular_start_at is None or calendar.regular_end_at is None:
            raise VerificationError("golden vector unexpectedly has closed calendar")
        result = scalar(
            container,
            f"""
select concat_ws('|',
  private.pit_calendar_identity_sha256_v1(
    {sql_text(calendar.provider)},
    {sql_text(calendar.market)},
    {sql_text(calendar_payload['session_date'])}::date
  ),
  private.pit_calendar_canonical_evidence_sha256_v1(
    {sql_text(calendar.provider)},
    {sql_text(calendar.market)},
    {sql_text(calendar_payload['session_date'])}::date,
    {str(calendar.is_open).lower()},
    {sql_text(calendar_payload['regular_start_at'])}::timestamptz,
    {sql_text(calendar_payload['regular_end_at'])}::timestamptz,
    {sql_text(calendar_payload['next_business_date'])}::date,
    {sql_text(calendar_payload['next_regular_start_at'])}::timestamptz,
    {sql_text(calendar_payload['next_regular_end_at'])}::timestamptz,
    {sql_text(calendar.provider_contract_sha256)}
  ),
  private.pit_daily_candle_timing_identity_sha256_v1(
    {sql_text(timing.candle_idempotency_key)},
    {sql_text(timing.calendar_idempotency_key)}
  ),
  private.pit_daily_candle_timing_evidence_sha256_v1(
    {sql_text(timing.provider)},
    {sql_text(timing.market)},
    {sql_text(timing.symbol)},
    {sql_text(timing.interval)},
    {str(timing.adjusted).lower()},
    {sql_text(timing_payload['session_date'])}::date,
    {sql_text(timing_payload['candle_provider_event_at'])}::timestamptz,
    {sql_text(timing_payload['regular_start_at'])}::timestamptz,
    {sql_text(timing_payload['regular_end_at'])}::timestamptz,
    {sql_text(timing_payload['next_business_date'])}::date,
    {sql_text(timing_payload['cutoff_at'])}::timestamptz,
    {sql_text(timing_payload['candle_observed_at'])}::timestamptz,
    {sql_text(timing_payload['calendar_observed_at'])}::timestamptz,
    {sql_text(timing_payload['evidence_available_at'])}::timestamptz,
    {sql_text(timing.candle_idempotency_key)},
    {sql_text(timing.calendar_idempotency_key)},
    {sql_text(timing.candle_provider_contract_sha256)},
    {sql_text(timing.calendar_provider_contract_sha256)},
    {sql_text(timing.candle_canonical_observation_sha256)},
    {sql_text(timing.calendar_canonical_evidence_sha256)}
  ),
  private.pit_canonical_timestamp_v1(
    {sql_text(timing_payload['candle_provider_event_at'])}::timestamptz
  )
);
""",
        )
        expected = "|".join(
            (
                calendar.idempotency_key,
                calendar.canonical_evidence_sha256,
                timing.idempotency_key,
                timing.canonical_timing_evidence_sha256,
                str(timing_payload["candle_provider_event_at"]),
            )
        )
        if result != expected:
            raise VerificationError(
                f"Python/SQL calendar/timing golden mismatch: {result} != {expected}"
            )
    print("PASS Python/SQL calendar, timing, KST/UTC and microsecond hashes")


def verify_catalog_and_acl(container: str) -> None:
    table_list = ",".join(sql_text(name) for name in PRIVATE_TABLES)
    append_only_list = ",".join(sql_text(name) for name in APPEND_ONLY_TABLES)
    result = scalar(
        container,
        f"""
select concat_ws('|',
  (select count(*) from pg_catalog.pg_class as c
   join pg_catalog.pg_namespace as n on n.oid=c.relnamespace
   where n.nspname='private' and c.relname in ({table_list})
     and c.relkind='r' and c.relrowsecurity),
  (select count(*) from pg_catalog.pg_trigger as t
   join pg_catalog.pg_class as c on c.oid=t.tgrelid
   join pg_catalog.pg_namespace as n on n.oid=c.relnamespace
   where n.nspname='private' and c.relname in ({append_only_list})
     and not t.tgisinternal),
  (select p.prosecdef from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
   )),
  (select p.proconfig=array['search_path=""']::text[] from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
   )),
  (select not p.prosecdef from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)'
   )),
  (select p.proconfig=array['search_path=""']::text[] from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)'
   )),
  has_function_privilege(
    'service_role',
    'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'authenticated',
    'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'anon',
    'worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)',
    'EXECUTE'
  ),
  (select pg_catalog.pg_get_userbyid(p.proowner)
     not in ('anon','authenticated','service_role','authenticator')
   from pg_catalog.pg_proc as p
   where p.oid=pg_catalog.to_regprocedure(
     'private.append_pit_daily_candle_timing_evidence_v1_impl(text,jsonb,jsonb)'
   )),
  (select count(*)=0
   from information_schema.role_table_grants
   where table_schema='private' and table_name in ({table_list})
     and grantee in ('anon','authenticated','service_role')),
  not exists (
    select 1
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid=p.pronamespace
    cross join lateral pg_catalog.aclexplode(
      coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))
    ) as acl
    where (
      (
        n.nspname='private' and p.proname in (
          'pit_calendar_identity_sha256_v1',
          'pit_calendar_canonical_evidence_sha256_v1',
          'pit_daily_candle_timing_identity_sha256_v1',
          'pit_daily_candle_timing_evidence_sha256_v1',
          'pit_daily_candle_timing_request_sha256_v1',
          'quarantine_pit_calendar_observation_v1',
          'quarantine_pit_daily_candle_timing_v1',
          'put_pit_daily_candle_timing_receipt_v1',
          'append_pit_daily_candle_timing_evidence_v1_impl'
        )
      ) or (
        n.nspname='worker_api'
        and p.proname='append_pit_daily_candle_timing_evidence_v1'
      )
    ) and acl.grantee=0 and acl.privilege_type='EXECUTE'
  ),
  not exists (
    select 1
    from pg_catalog.pg_proc as p
    join pg_catalog.pg_namespace as n on n.oid=p.pronamespace
    where n.nspname='private' and p.proname in (
      'pit_calendar_identity_sha256_v1',
      'pit_calendar_canonical_evidence_sha256_v1',
      'pit_daily_candle_timing_identity_sha256_v1',
      'pit_daily_candle_timing_evidence_sha256_v1',
      'pit_daily_candle_timing_request_sha256_v1',
      'quarantine_pit_calendar_observation_v1',
      'quarantine_pit_daily_candle_timing_v1',
      'put_pit_daily_candle_timing_receipt_v1',
      'append_pit_daily_candle_timing_evidence_v1_impl'
    ) and (
      not p.prosecdef
      or p.proconfig is distinct from array['search_path=""']::text[]
    )
  ),
  not exists (
    select 1
    from pg_catalog.pg_constraint as con
    join pg_catalog.pg_class as rel on rel.oid=con.conrelid
    join pg_catalog.pg_namespace as n on n.oid=rel.relnamespace
    where con.contype='f' and n.nspname='private'
      and rel.relname in ({table_list})
      and not exists (
        select 1
        from pg_catalog.pg_index as idx
        where idx.indrelid=con.conrelid and idx.indisvalid
          and (
            select pg_catalog.array_agg(key.attnum order by key.ordinality)
            from pg_catalog.unnest(idx.indkey)
              with ordinality as key(attnum, ordinality)
            where key.ordinality <= pg_catalog.cardinality(con.conkey)
          ) = con.conkey
      )
  )
);
""",
    )
    if result != "8|6|t|t|t|t|t|t|t|t|t|t|t|t":
        raise VerificationError(f"PIT timing catalog/ACL mismatch: {result}")

    first_columns = {
        table: _first_column(container, table) for table in PRIVATE_TABLES
    }
    for role in ("anon", "authenticated", "service_role"):
        for table in PRIVATE_TABLES:
            first_column = first_columns[table]
            for statement in (
                f"select count(*) from private.{table};",
                f"insert into private.{table} default values;",
                f"update private.{table} set {first_column}={first_column} "
                "where false;",
                f"delete from private.{table} where false;",
            ):
                expect_failure(
                    container,
                    f"set role {role}; {statement}",
                    "permission denied",
                )

    sample_candle, sample_calendar, sample_timing = fixture(
        day_offset=40,
        symbol="199990",
    )
    for role in ("anon", "authenticated"):
        expect_failure(
            container,
            jwt_claim_sql(WORKER_ID, role=role)
            + "select * from worker_api.append_pit_daily_candle_timing_evidence_v1("
            + f"{sql_text(request_key(f'acl-{role}'))},"
            + f"{jsonb_literal(sample_calendar.to_payload(), 'calendar')},"
            + f"{jsonb_literal(sample_timing.to_payload(), 'timing')});",
            "permission denied",
        )
    del sample_candle
    print("PASS PIT timing RLS, FK indexes, direct ACL denial and RPC boundary")


def _first_column(container: str, table: str) -> str:
    column = scalar(
        container,
        f"""
select a.attname
from pg_catalog.pg_attribute as a
where a.attrelid='private.{table}'::regclass
  and a.attnum > 0 and not a.attisdropped
order by a.attnum
limit 1;
""",
    )
    if not column:
        raise VerificationError(f"PIT timing table has no columns: {table}")
    return column


def verify_concurrent_exact_delivery(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=0, symbol="100001")
    append_candle(container, candle)
    key = request_key("concurrent-exact")
    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = list(
            pool.map(
                lambda _index: append_timing(container, key, calendar, timing),
                range(32),
            )
        )

    if (
        any(receipt != receipts[0] for receipt in receipts[1:])
        or {receipt.get("status") for receipt in receipts} != {"stored"}
        or {receipt.get("calendar_inserted") for receipt in receipts} != {True}
        or {receipt.get("timing_inserted") for receipt in receipts} != {True}
        or {receipt.get("calendar_revision") for receipt in receipts} != {1}
        or {receipt.get("timing_revision") for receipt in receipts} != {1}
        or table_count(container, "pit_calendar_content_revisions") != 1
        or table_count(container, "pit_daily_candle_timing_revisions") != 1
        or table_count(container, "pit_daily_candle_timing_request_ledger") != 1
        or table_count(container, "pit_daily_candle_timing_quarantine") != 0
    ):
        raise VerificationError(f"concurrent exact PIT timing mismatch: {receipts}")
    print("PASS 32-way same request returns one byte-semantic durable receipt")

    independent_candle, independent_calendar, independent_timing = fixture(
        day_offset=13,
        symbol="100014",
    )
    append_candle(container, independent_candle)
    independent_keys = [request_key(f"concurrent-independent-{index}") for index in range(16)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        independent = list(
            pool.map(
                lambda item: append_timing(
                    container,
                    item,
                    independent_calendar,
                    independent_timing,
                ),
                independent_keys,
            )
        )
    if (
        [receipt.get("status") for receipt in independent].count("stored") != 1
        or [receipt.get("status") for receipt in independent].count("replayed") != 15
        or [receipt.get("calendar_inserted") for receipt in independent].count(True)
        != 1
        or [receipt.get("timing_inserted") for receipt in independent].count(True) != 1
        or table_count(
            container,
            "pit_daily_candle_timing_revisions",
            f"timing_idempotency_key={sql_text(independent_timing.idempotency_key)}",
        )
        != 1
    ):
        raise VerificationError(
            f"independent-key concurrent timing mismatch: {independent}"
        )
    print("PASS distinct request keys serialize one logical timing revision")


def verify_replay_and_corrections(container: str) -> None:
    candle_a, calendar_a, timing_a = fixture(day_offset=1, symbol="100002")
    append_candle(container, candle_a)
    first = append_timing(
        container,
        request_key("correction-a"),
        calendar_a,
        timing_a,
    )
    assert_accepted(
        first,
        timing_a,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )

    exact = append_timing(
        container,
        request_key("correction-a-exact-new-request"),
        calendar_a,
        timing_a,
    )
    assert_accepted(
        exact,
        timing_a,
        status="replayed",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=False,
        timing_inserted=False,
    )

    _, calendar_b, timing_b = fixture(
        day_offset=1,
        symbol="100002",
        calendar_observed_minutes=1,
        regular_end_delta_minutes=-5,
    )
    second = append_timing(
        container,
        request_key("correction-b"),
        calendar_b,
        timing_b,
    )
    assert_accepted(
        second,
        timing_b,
        status="stored",
        calendar_revision=2,
        timing_revision=2,
        calendar_inserted=True,
        timing_inserted=True,
    )

    candle_c, calendar_c, timing_c = fixture(
        day_offset=1,
        symbol="100002",
        close_krw=72_100,
        candle_observed_minutes=2,
        calendar_observed_minutes=1,
        regular_end_delta_minutes=-5,
    )
    candle_receipt = append_candle(container, candle_c)
    if candle_receipt.get("revision") != 2:
        raise VerificationError(f"candle correction was not revision two: {candle_receipt}")
    third = append_timing(
        container,
        request_key("correction-c"),
        calendar_c,
        timing_c,
    )
    assert_accepted(
        third,
        timing_c,
        status="stored",
        calendar_revision=2,
        timing_revision=3,
        calendar_inserted=False,
        timing_inserted=True,
    )
    print("PASS exact replay plus calendar and candle-bound timing corrections")


def verify_exact_reobservation_creates_occurrence(container: str) -> None:
    candle, calendar, timing = fixture(day_offset=14, symbol="100015")
    append_candle(container, candle)
    append_timing(
        container,
        request_key("exact-reobservation-base"),
        calendar,
        timing,
    )
    _, later_calendar, later_timing = fixture(
        day_offset=14,
        symbol="100015",
        calendar_observed_minutes=5,
    )
    accepted = append_timing(
        container,
        request_key("exact-reobservation-later-clock"),
        later_calendar,
        later_timing,
    )
    assert_accepted(
        accepted,
        later_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )
    state = scalar(
        container,
        f"""
select concat_ws('|',
  private.pit_canonical_timestamp_v1(head.last_seen_observed_at),
  (select count(*) from private.pit_calendar_content_revisions as revision
   where revision.calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_calendar_observation_occurrences as occurrence
   where occurrence.calendar_idempotency_key={sql_text(calendar.idempotency_key)}),
  (select count(*) from private.pit_daily_candle_timing_revisions as revision
   where revision.timing_idempotency_key={sql_text(timing.idempotency_key)})
)
from private.pit_calendar_stream_heads as head
where head.calendar_idempotency_key={sql_text(calendar.idempotency_key)};
""",
    )
    expected_state = f"{later_calendar.to_payload()['observed_at']}|1|2|2"
    if state != expected_state:
        raise VerificationError(
            f"exact re-observation occurrence state mismatch: {state}"
        )

    retry = append_timing(
        container,
        request_key("exact-reobservation-later-clock"),
        later_calendar,
        later_timing,
    )
    assert_accepted(
        retry,
        later_timing,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )
    if retry != accepted:
        raise VerificationError(
            f"exact re-observation request replay changed receipt: {accepted} / {retry}"
        )
    print("PASS exact re-observation appends an occurrence and exact timing binding")


def verify_calendar_revision_guards(container: str) -> None:
    # Regression after a later accepted observation must quarantine durably.
    candle_r, calendar_r, timing_r = fixture(
        day_offset=2,
        symbol="100003",
        calendar_observed_minutes=2,
    )
    append_candle(container, candle_r)
    append_timing(container, request_key("calendar-regression-base"), calendar_r, timing_r)
    _, stale_calendar, stale_timing = fixture(
        day_offset=2,
        symbol="100003",
        calendar_observed_minutes=1,
        regular_end_delta_minutes=-5,
    )
    stale = append_timing(
        container,
        request_key("calendar-regression-candidate"),
        stale_calendar,
        stale_timing,
    )
    stale_id = assert_quarantine(stale, "pit_calendar_observation_time_regressed")
    stale_retry = append_timing(
        container,
        request_key("calendar-regression-candidate"),
        stale_calendar,
        stale_timing,
    )
    assert_quarantine(
        stale_retry,
        "pit_calendar_observation_time_regressed",
        previous_id=stale_id,
    )

    # A changed calendar at the exact head clock cannot mint a new revision.
    candle_s, calendar_s, timing_s = fixture(day_offset=3, symbol="100004")
    append_candle(container, candle_s)
    append_timing(container, request_key("calendar-same-clock-base"), calendar_s, timing_s)
    _, same_clock_calendar, same_clock_timing = fixture(
        day_offset=3,
        symbol="100004",
        regular_end_delta_minutes=-5,
    )
    same_clock = append_timing(
        container,
        request_key("calendar-same-clock-candidate"),
        same_clock_calendar,
        same_clock_timing,
    )
    assert_quarantine(same_clock, "pit_calendar_revision_time_not_increasing")

    # A -> B -> A is ambiguous even when the returning A has a later clock.
    candle_a, calendar_a, timing_a = fixture(day_offset=4, symbol="100005")
    append_candle(container, candle_a)
    append_timing(container, request_key("calendar-aba-a"), calendar_a, timing_a)
    _, calendar_b, timing_b = fixture(
        day_offset=4,
        symbol="100005",
        calendar_observed_minutes=1,
        regular_end_delta_minutes=-5,
    )
    append_timing(container, request_key("calendar-aba-b"), calendar_b, timing_b)
    _, calendar_a_again, timing_a_again = fixture(
        day_offset=4,
        symbol="100005",
        calendar_observed_minutes=2,
    )
    recurrence = append_timing(
        container,
        request_key("calendar-aba-a-again"),
        calendar_a_again,
        timing_a_again,
    )
    assert_quarantine(
        recurrence,
        "pit_calendar_historical_hash_recurrence_ambiguous",
    )
    print("PASS calendar regression, same-clock and A-B-A guards")


def verify_timing_revision_guards(container: str) -> None:
    # Persist both candle revisions first, then submit their timing in reverse
    # evidence-clock order.  Exact historical candle binding must be possible,
    # but the timing stream must reject the regressed new hash.
    candle_old, calendar, timing_old = fixture(day_offset=10, symbol="100011")
    candle_new, _, timing_new = fixture(
        day_offset=10,
        symbol="100011",
        close_krw=72_100,
        candle_observed_minutes=2,
    )
    append_candle(container, candle_old)
    append_candle(container, candle_new)
    append_timing(
        container,
        request_key("timing-regression-new-first"),
        calendar,
        timing_new,
    )
    regressed = append_timing(
        container,
        request_key("timing-regression-old-second"),
        calendar,
        timing_old,
    )
    assert_quarantine(regressed, "pit_timing_observation_time_regressed")

    # Different exact candle revisions can produce distinct timing hashes at
    # the same availability clock when calendar evidence is the later source.
    candle_a, calendar_late, timing_a = fixture(
        day_offset=11,
        symbol="100012",
        calendar_observed_minutes=2,
    )
    candle_b, _, timing_b = fixture(
        day_offset=11,
        symbol="100012",
        close_krw=72_100,
        candle_observed_minutes=1,
        calendar_observed_minutes=2,
    )
    append_candle(container, candle_a)
    append_candle(container, candle_b)
    append_timing(
        container,
        request_key("timing-same-clock-a"),
        calendar_late,
        timing_a,
    )
    same_clock = append_timing(
        container,
        request_key("timing-same-clock-b"),
        calendar_late,
        timing_b,
    )
    assert_accepted(
        same_clock,
        timing_b,
        status="stored",
        calendar_revision=1,
        timing_revision=2,
        calendar_inserted=False,
        timing_inserted=True,
    )

    candle_x, calendar_x, timing_x = fixture(day_offset=12, symbol="100013")
    candle_y, _, timing_y = fixture(
        day_offset=12,
        symbol="100013",
        close_krw=72_100,
        candle_observed_minutes=1,
    )
    append_candle(container, candle_x)
    append_timing(container, request_key("timing-aba-a"), calendar_x, timing_x)
    append_candle(container, candle_y)
    append_timing(container, request_key("timing-aba-b"), calendar_x, timing_y)
    recurrence = append_timing(
        container,
        request_key("timing-aba-a-again"),
        calendar_x,
        timing_x,
    )
    assert_quarantine(
        recurrence,
        "pit_timing_historical_hash_recurrence_ambiguous",
    )
    print("PASS timing regression, same-availability advance and A-B-A guards")


def verify_request_idempotency_conflict(container: str) -> None:
    candle_a, calendar_a, timing_a = fixture(day_offset=5, symbol="100006")
    append_candle(container, candle_a)
    key = request_key("request-conflict")
    append_timing(container, key, calendar_a, timing_a)

    candle_b, calendar_b, timing_b = fixture(
        day_offset=5,
        symbol="100006",
        close_krw=72_100,
        candle_observed_minutes=1,
    )
    append_candle(container, candle_b)
    conflict = append_timing(container, key, calendar_b, timing_b)
    quarantine_id = assert_quarantine(
        conflict,
        "pit_timing_request_idempotency_conflict",
    )
    retried = append_timing(container, key, calendar_b, timing_b)
    assert_quarantine(
        retried,
        "pit_timing_request_idempotency_conflict",
        previous_id=quarantine_id,
    )
    accepted = table_count(
        container,
        "pit_daily_candle_timing_revisions",
        f"timing_idempotency_key={sql_text(timing_a.idempotency_key)}",
    )
    if accepted != 1:
        raise VerificationError(f"request conflict changed accepted timing: {accepted}")
    print("PASS request retry and conflicting-payload quarantine are stable")


def verify_missing_and_forged_sources(container: str) -> None:
    # A calendar payload is not sufficient proof when its candle revision is absent.
    _, missing_calendar, missing_timing = fixture(day_offset=6, symbol="100007")
    missing = append_timing(
        container,
        request_key("missing-candle-source"),
        missing_calendar,
        missing_timing,
    )
    missing_id = assert_quarantine(missing, "pit_timing_candle_revision_missing")
    missing_retry = append_timing(
        container,
        request_key("missing-candle-source"),
        missing_calendar,
        missing_timing,
    )
    assert_quarantine(
        missing_retry,
        "pit_timing_candle_revision_missing",
        previous_id=missing_id,
    )
    if table_count(
        container,
        "pit_daily_candle_timing_revisions",
        f"timing_idempotency_key={sql_text(missing_timing.idempotency_key)}",
    ):
        raise VerificationError("missing candle source created accepted timing")

    candle, calendar, timing = fixture(day_offset=7, symbol="100008")
    append_candle(container, candle)
    forged_source = timing.to_payload()
    forged_source["symbol"] = "999999"
    source_receipt = append_payloads(
        container,
        request_key("forged-source-binding"),
        calendar.to_payload(),
        forged_source,
    )
    assert_quarantine(source_receipt, "pit_timing_source_binding_mismatch")

    candle_av, calendar_av, timing_av = fixture(day_offset=8, symbol="100009")
    append_candle(container, candle_av)
    forged_available = timing_av.to_payload()
    forged_available["evidence_available_at"] = (
        timing_av.evidence_available_at + timedelta(seconds=1)
    ).astimezone(UTC).isoformat().replace("+00:00", "Z")
    available_receipt = append_payloads(
        container,
        request_key("forged-available-at"),
        calendar_av.to_payload(),
        forged_available,
    )
    assert_quarantine(available_receipt, "pit_timing_available_at_mismatch")

    candle_sha, calendar_sha, timing_sha = fixture(day_offset=9, symbol="100010")
    append_candle(container, candle_sha)
    forged_sha = timing_sha.to_payload()
    forged_sha["canonical_timing_evidence_sha256"] = "0" * 64
    sha_receipt = append_payloads(
        container,
        request_key("forged-timing-sha"),
        calendar_sha.to_payload(),
        forged_sha,
    )
    assert_quarantine(sha_receipt, "pit_timing_canonical_sha256_mismatch")

    for receipt in (source_receipt, available_receipt, sha_receipt):
        if receipt.get("calendar_inserted") is not False:
            raise VerificationError(
                f"forged timing reported a calendar insert: {receipt}"
            )
    for calendar_evidence in (calendar, calendar_av, calendar_sha):
        calendar_counts = scalar(
            container,
            f"""
select concat_ws('|',
  (select count(*) from private.pit_calendar_stream_heads as head
   where head.calendar_idempotency_key=
     {sql_text(calendar_evidence.idempotency_key)}),
  (select count(*) from private.pit_calendar_content_revisions as revision
   where revision.calendar_idempotency_key=
     {sql_text(calendar_evidence.idempotency_key)})
);
""",
        )
        if calendar_counts != "0|0":
            raise VerificationError(
                "forged timing mutated calendar state: "
                f"{calendar_evidence.idempotency_key}={calendar_counts}"
            )
    for evidence in (timing, timing_av, timing_sha):
        accepted = table_count(
            container,
            "pit_daily_candle_timing_revisions",
            f"timing_idempotency_key={sql_text(evidence.idempotency_key)}",
        )
        if accepted:
            raise VerificationError(
                f"forged timing created accepted revision: {evidence.idempotency_key}"
            )
    print("PASS missing and forged source/hash/availability inputs stay unaccepted")


def verify_append_only_evidence(container: str) -> None:
    for table in APPEND_ONLY_TABLES:
        first_column = _first_column(container, table)
        expect_failure(
            container,
            f"update private.{table} set {first_column}={first_column} where true;",
            "append_only_table_mutation_forbidden",
        )
        expect_failure(
            container,
            f"delete from private.{table} where true;",
            "append_only_table_mutation_forbidden",
        )
    print("PASS calendar, timing, request and quarantine evidence is append-only")


def apply_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("PIT daily candle timing migration is missing") from exc

    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    before = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    )
    if not before:
        raise VerificationError("populated upgrade fixture is missing bot settings")

    candle_before, calendar_before, timing_before = fixture(
        day_offset=30,
        symbol="130001",
    )
    append_candle(container, candle_before)
    psql(container, target.read_text(encoding="utf-8"))
    first = append_timing(
        container,
        request_key("populated-upgrade-at-target"),
        calendar_before,
        timing_before,
    )
    assert_accepted(
        first,
        timing_before,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )
    verify_catalog_and_acl(container)

    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))

    after = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    )
    if after != before:
        raise VerificationError("PIT timing migration path changed populated bot settings")

    verify_catalog_and_acl(container)
    candle_after, calendar_after, timing_after = fixture(
        day_offset=31,
        symbol="130002",
    )
    append_candle(container, candle_after)
    future_receipt = append_timing(
        container,
        request_key("populated-upgrade-after-future-migrations"),
        calendar_after,
        timing_after,
    )
    assert_accepted(
        future_receipt,
        timing_after,
        status="stored",
        calendar_revision=1,
        timing_revision=1,
        calendar_inserted=True,
        timing_inserted=True,
    )
    print("PASS populated target upgrade and all future migrations converge safely")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-timing-fresh-{suffix}"
    upgrade = f"msp-pit-timing-upgrade-{suffix}"
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
        verify_catalog_and_acl(fresh)
        verify_python_sql_golden_vectors(fresh)
        verify_concurrent_exact_delivery(fresh)
        verify_replay_and_corrections(fresh)
        verify_exact_reobservation_creates_occurrence(fresh)
        verify_calendar_revision_guards(fresh)
        verify_timing_revision_guards(fresh)
        verify_request_idempotency_conflict(fresh)
        verify_missing_and_forged_sources(fresh)
        verify_append_only_evidence(fresh)
        apply_populated_upgrade(upgrade)
        print("FINAL=PASS pit_daily_candle_timing_store_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
