#!/usr/bin/env python3
"""Verify the durable PIT candle revision store in disposable PostgreSQL.

This verifier never connects to a hosted project. It covers the fresh and
populated-upgrade paths, strict ACLs, canonical Python/SQL hashes, concurrent
delivery, corrections, exact replay, and durable quarantine behavior.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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

from app.domain.market_data.point_in_time import PointInTimeCandleV1  # noqa: E402

MIGRATION_NAME = "20260719001947_pit_candle_revision_store.sql"
WORKER_ID = "72727272-7272-4272-8272-727272727272"
CONTRACT_SHA256 = "a" * 64
EVENT_AT = datetime(2026, 3, 24, 0, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)


def candle(
    *,
    symbol: str,
    observed_at: datetime = OBSERVED_AT,
    close_krw: int = 72_000,
    high_krw: int = 72_300,
    adjusted: bool = True,
    provider_event_at: datetime = EVENT_AT,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider="toss",
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=adjusted,
        provider_event_at=provider_event_at,
        observed_at=observed_at,
        currency="KRW",
        open_krw=71_600,
        high_krw=high_krw,
        low_krw=71_500,
        close_krw=close_krw,
        volume=3_521_000,
        provider_contract_sha256=CONTRACT_SHA256,
    )


def payload_literal(value: PointInTimeCandleV1) -> str:
    payload = json.dumps(
        value.to_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if "$pit$" in payload:
        raise VerificationError("unexpected PIT payload delimiter")
    return f"$pit${payload}$pit$::jsonb"


def append(container: str, value: PointInTimeCandleV1) -> dict[str, object]:
    result = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"""
select row_to_json(receipt)
from worker_api.append_pit_candle_observation_v1(
  {payload_literal(value)}
) as receipt;
""",
    ).stdout.strip().splitlines()
    if not result:
        raise VerificationError("PIT candle RPC returned no receipt")
    parsed = json.loads(result[-1])
    if not isinstance(parsed, dict):
        raise VerificationError(f"PIT candle receipt is not an object: {parsed}")
    return parsed


def count_rows(container: str, table: str, identity: str) -> int:
    result = psql(
        container,
        f"""
select count(*)
from private.{table}
where idempotency_key='{identity}';
""",
    ).stdout.strip()
    return int(result)


def verify_catalog_and_acl(container: str) -> None:
    result = psql(
        container,
        """
select concat_ws('|',
  (select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
   where n.nspname='private'
     and c.relname in (
       'pit_candle_stream_heads',
       'pit_candle_observation_revisions',
       'pit_candle_observation_quarantine'
     ) and c.relrowsecurity),
  (select count(*) from pg_trigger t
   where t.tgrelid in (
     'private.pit_candle_observation_revisions'::regclass,
     'private.pit_candle_observation_quarantine'::regclass
   ) and not t.tgisinternal),
  (select p.prosecdef from pg_proc p
   where p.oid='private.append_pit_candle_observation_v1_impl(jsonb)'::regprocedure),
  (select p.proconfig=array['search_path=""']::text[] from pg_proc p
   where p.oid='private.append_pit_candle_observation_v1_impl(jsonb)'::regprocedure),
  (select not p.prosecdef from pg_proc p
   where p.oid='worker_api.append_pit_candle_observation_v1(jsonb)'::regprocedure),
  (select p.proconfig=array['search_path=""']::text[] from pg_proc p
   where p.oid='worker_api.append_pit_candle_observation_v1(jsonb)'::regprocedure),
  has_function_privilege(
    'service_role',
    'worker_api.append_pit_candle_observation_v1(jsonb)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'authenticated',
    'worker_api.append_pit_candle_observation_v1(jsonb)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'anon',
    'worker_api.append_pit_candle_observation_v1(jsonb)',
    'EXECUTE'
  ),
  not has_table_privilege(
    'service_role', 'private.pit_candle_stream_heads', 'SELECT,INSERT,UPDATE,DELETE'
  ),
  not has_table_privilege(
    'authenticated',
    'private.pit_candle_observation_revisions',
    'SELECT,INSERT,UPDATE,DELETE'
  ),
  not has_table_privilege(
    'anon',
    'private.pit_candle_observation_quarantine',
    'SELECT,INSERT,UPDATE,DELETE'
  ),
  not exists (
    select 1
    from pg_proc p
    cross join lateral aclexplode(
      coalesce(p.proacl, acldefault('f', p.proowner))
    ) acl
    where p.oid in (
      'private.pit_canonical_timestamp_v1(timestamptz)'::regprocedure,
      'private.pit_sha256_text_v1(text)'::regprocedure,
      'private.pit_candle_identity_sha256_v1(text,text,text,text,boolean,timestamptz)'::regprocedure,
      'private.pit_candle_observation_sha256_v1(text,text,text,text,boolean,timestamptz,text,numeric,numeric,numeric,numeric,numeric,text)'::regprocedure,
      'private.quarantine_pit_candle_observation_v1(text,text,text,timestamptz,jsonb,bigint,text,timestamptz)'::regprocedure,
      'private.append_pit_candle_observation_v1_impl(jsonb)'::regprocedure,
      'worker_api.append_pit_candle_observation_v1(jsonb)'::regprocedure
    )
      and acl.grantee=0
      and acl.privilege_type='EXECUTE'
  ),
  (select pg_get_userbyid(p.proowner)
     not in ('anon','authenticated','service_role','authenticator')
   from pg_proc p
   where p.oid='private.append_pit_candle_observation_v1_impl(jsonb)'::regprocedure),
  (select count(*)=0
   from information_schema.role_table_grants
   where table_schema='private'
     and table_name in (
       'pit_candle_stream_heads',
       'pit_candle_observation_revisions',
       'pit_candle_observation_quarantine'
     )
     and grantee in ('anon','authenticated','service_role'))
);
""",
    ).stdout.strip()
    if result != "3|2|t|t|t|t|t|t|t|t|t|t|t|t|t":
        raise VerificationError(f"PIT candle catalog/ACL mismatch: {result}")

    tables = (
        "pit_candle_stream_heads",
        "pit_candle_observation_revisions",
        "pit_candle_observation_quarantine",
    )
    zero_key = "0" * 64
    for role in ("anon", "authenticated", "service_role"):
        for table in tables:
            statements = (
                f"select count(*) from private.{table};",
                f"insert into private.{table} (idempotency_key) "
                f"values ('{zero_key}');",
                f"update private.{table} set idempotency_key=idempotency_key "
                "where false;",
                f"delete from private.{table} where false;",
            )
            for statement in statements:
                expect_failure(
                    container,
                    f"set role {role}; {statement}",
                    "permission denied",
                )
    for role in ("anon", "authenticated"):
        expect_failure(
            container,
            jwt_claim_sql(WORKER_ID, role=role)
            + f"select * from worker_api.append_pit_candle_observation_v1("
            f"{payload_literal(candle(symbol='111111'))});",
            "permission denied",
        )
    print("PASS PIT candle RLS, direct ACL denial and service-only RPC boundary")


def verify_python_sql_golden_vectors(container: str) -> None:
    kst = timezone(timedelta(hours=9))
    huge_price = 10**80
    vectors = (
        candle(symbol="101010", adjusted=False),
        candle(
            symbol="101011",
            provider_event_at=datetime(
                2026,
                3,
                24,
                9,
                0,
                0,
                123456,
                tzinfo=kst,
            ),
            observed_at=datetime(
                2026,
                3,
                25,
                9,
                0,
                0,
                654321,
                tzinfo=kst,
            ),
        ),
        candle(
            symbol="101012",
            close_krw=huge_price,
            high_krw=huge_price,
        ),
    )
    for value in vectors:
        payload = value.to_payload()
        result = psql(
            container,
            f"""
select concat_ws('|',
  private.pit_candle_identity_sha256_v1(
    '{value.provider}', '{value.symbol}', '{value.market}', '{value.interval}',
    {str(value.adjusted).lower()}, '{payload['provider_event_at']}'::timestamptz
  ),
  private.pit_candle_observation_sha256_v1(
    '{value.provider}', '{value.symbol}', '{value.market}', '{value.interval}',
    {str(value.adjusted).lower()}, '{payload['provider_event_at']}'::timestamptz,
    '{value.currency}', {value.open_krw}, {value.high_krw}, {value.low_krw},
    {value.close_krw}, {value.volume}, '{value.provider_contract_sha256}'
  )
);
""",
        ).stdout.strip()
        expected = (
            f"{value.idempotency_key}|"
            f"{value.canonical_observation_sha256}"
        )
        if result != expected:
            raise VerificationError(
                f"Python/SQL PIT candle hash mismatch: {result} != {expected}"
            )
    print("PASS Python/SQL canonical UTC, boolean, integer and hash vectors")


def verify_concurrent_exact_delivery(container: str) -> None:
    value = candle(symbol="005930")
    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = list(pool.map(lambda _index: append(container, value), range(32)))

    statuses = [receipt.get("status") for receipt in receipts]
    inserted = [receipt.get("inserted") for receipt in receipts]
    if (
        statuses.count("stored") != 1
        or statuses.count("replayed") != 31
        or inserted.count(True) != 1
        or {receipt.get("revision") for receipt in receipts} != {1}
        or count_rows(
            container,
            "pit_candle_observation_revisions",
            value.idempotency_key,
        )
        != 1
        or count_rows(
            container,
            "pit_candle_observation_quarantine",
            value.idempotency_key,
        )
        != 0
    ):
        raise VerificationError(f"concurrent exact delivery mismatch: {receipts}")
    print("PASS 32-way exact delivery inserts one durable revision")


def verify_concurrent_same_clock_conflict(container: str) -> None:
    first = candle(symbol="000660", close_krw=72_000)
    second = candle(symbol="000660", close_krw=72_100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda value: append(container, value), (first, second)))

    statuses = {receipt.get("status") for receipt in receipts}
    if statuses != {"stored", "quarantined"}:
        raise VerificationError(f"same-clock conflict mismatch: {receipts}")
    quarantined = next(
        receipt for receipt in receipts if receipt.get("status") == "quarantined"
    )
    losing = (
        first
        if quarantined.get("canonical_observation_sha256")
        == first.canonical_observation_sha256
        else second
    )
    replayed_quarantine = append(container, losing)
    if (
        quarantined.get("reason_code")
        != "candle_observation_store_revision_time_not_increasing"
        or replayed_quarantine.get("quarantine_id")
        != quarantined.get("quarantine_id")
        or count_rows(
            container,
            "pit_candle_observation_revisions",
            first.idempotency_key,
        )
        != 1
        or count_rows(
            container,
            "pit_candle_observation_quarantine",
            first.idempotency_key,
        )
        != 1
    ):
        raise VerificationError(
            f"same-clock quarantine durability mismatch: {receipts} / "
            f"{replayed_quarantine}"
        )
    print("PASS same-clock conflict stores one revision and stable quarantine")


def verify_revision_and_quarantine_semantics(container: str) -> None:
    sequence_a = candle(symbol="003550")
    sequence_b = candle(
        symbol="003550",
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        close_krw=72_100,
    )
    sequence_c = candle(
        symbol="003550",
        observed_at=OBSERVED_AT + timedelta(minutes=2),
        close_krw=72_200,
    )
    sequence_receipts = [
        append(container, value) for value in (sequence_a, sequence_b, sequence_c)
    ]
    if (
        [receipt.get("revision") for receipt in sequence_receipts] != [1, 2, 3]
        or count_rows(
            container,
            "pit_candle_observation_revisions",
            sequence_a.idempotency_key,
        )
        != 3
    ):
        raise VerificationError(
            f"ordered correction revision mismatch: {sequence_receipts}"
        )

    first = candle(symbol="035420")
    later_replay = candle(
        symbol="035420",
        observed_at=OBSERVED_AT + timedelta(minutes=10),
    )
    stale_correction = candle(
        symbol="035420",
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
    )
    if append(container, first).get("revision") != 1:
        raise VerificationError("initial revision was not one")
    replay_receipt = append(container, later_replay)
    stale_receipt = append(container, stale_correction)
    if (
        replay_receipt.get("status") != "replayed"
        or stale_receipt.get("status") != "quarantined"
        or stale_receipt.get("reason_code")
        != "candle_observation_store_observation_time_regressed"
        or count_rows(
            container,
            "pit_candle_observation_revisions",
            first.idempotency_key,
        )
        != 1
    ):
        raise VerificationError(
            f"later replay/stale correction mismatch: {replay_receipt} / "
            f"{stale_receipt}"
        )

    a = candle(symbol="051910")
    b = candle(
        symbol="051910",
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
    )
    a_again = candle(
        symbol="051910",
        observed_at=OBSERVED_AT + timedelta(minutes=10),
    )
    first_receipt = append(container, a)
    second_receipt = append(container, b)
    recurrence = append(container, a_again)
    if (
        first_receipt.get("revision") != 1
        or second_receipt.get("revision") != 2
        or recurrence.get("status") != "quarantined"
        or recurrence.get("reason_code")
        != "candle_observation_store_historical_hash_recurrence_ambiguous"
        or count_rows(
            container,
            "pit_candle_observation_revisions",
            a.idempotency_key,
        )
        != 2
    ):
        raise VerificationError(
            f"A-B-A recurrence mismatch: {first_receipt} / {second_receipt} / "
            f"{recurrence}"
        )

    expect_failure(
        container,
        "update private.pit_candle_observation_revisions set revision=revision "
        f"where idempotency_key='{a.idempotency_key}';",
        "append_only_table_mutation_forbidden",
    )
    expect_failure(
        container,
        "delete from private.pit_candle_observation_quarantine "
        f"where idempotency_key='{a.idempotency_key}';",
        "append_only_table_mutation_forbidden",
    )
    print(
        "PASS ordered revisions, replay clock, A-B-A and append-only evidence"
    )


def verify_invalid_hash_has_zero_mutation(container: str) -> None:
    value = candle(symbol="068270")
    payload = value.to_payload()
    payload["canonical_observation_sha256"] = "0" * 64
    raw = json.dumps(payload, separators=(",", ":"))
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + "select * from worker_api.append_pit_candle_observation_v1("
        + f"$pit${raw}$pit$::jsonb);",
        "pit_candle_canonical_observation_sha256_mismatch",
    )
    counts = psql(
        container,
        f"""
select concat_ws('|',
  (select count(*) from private.pit_candle_stream_heads
   where idempotency_key='{value.idempotency_key}'),
  (select count(*) from private.pit_candle_observation_revisions
   where idempotency_key='{value.idempotency_key}'),
  (select count(*) from private.pit_candle_observation_quarantine
   where idempotency_key='{value.idempotency_key}')
);
""",
    ).stdout.strip()
    if counts != "0|0|0":
        raise VerificationError(f"invalid hash mutated PIT store: {counts}")
    print("PASS malformed canonical hash fails with zero mutation")


def apply_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("PIT candle migration is missing") from exc
    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))
    before = psql(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    ).stdout.strip()
    if not before:
        raise VerificationError("populated upgrade fixture is missing bot settings")
    psql(container, target.read_text(encoding="utf-8"))
    after = psql(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    ).stdout.strip()
    if after != before:
        raise VerificationError("PIT candle migration changed populated bot settings")
    receipt = append(container, candle(symbol="207940"))
    if receipt.get("status") != "stored" or receipt.get("revision") != 1:
        raise VerificationError(f"populated upgrade RPC mismatch: {receipt}")
    verify_catalog_and_acl(container)
    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))
    print("PASS populated pre-migration upgrade preserves existing safety state")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-candle-fresh-{suffix}"
    upgrade = f"msp-pit-candle-upgrade-{suffix}"
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
        verify_concurrent_same_clock_conflict(fresh)
        verify_revision_and_quarantine_semantics(fresh)
        verify_invalid_hash_has_zero_mutation(fresh)
        apply_populated_upgrade(upgrade)
        print("FINAL=PASS pit_candle_revision_store_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
