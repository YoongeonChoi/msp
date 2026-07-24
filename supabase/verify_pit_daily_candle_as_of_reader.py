#!/usr/bin/env python3
"""Verify the durable PIT daily-candle as-of reader in disposable PostgreSQL.

The verifier never connects to a hosted project. It proves the exact RPC
envelope, immutable occurrence lineage, semantic cutoff, cursor snapshot,
pagination, ACL, zero-write, and forward-upgrade contracts on PostgreSQL 17.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
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
    BASE_SESSION_DATE,
    WORKER_ID,
    append_timing,
    fixture,
    jsonb_literal,
    request_key,
    scalar,
    sql_text,
)
from verify_pit_source_observation_occurrence_store import (  # noqa: E402
    append_candle_receipt,
    reobserve_candle,
)

from app.domain.market_data.daily_candle_as_of import (  # noqa: E402
    select_daily_candles_as_of,
)
from app.domain.market_data.daily_candle_timing import (  # noqa: E402
    PointInTimeDailyCandleTimingEvidenceV1,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import (  # noqa: E402
    PointInTimeCandleV1,
)
from app.domain.market_data.point_in_time_calendar import (  # noqa: E402
    PointInTimeKrDailySessionV1,
)

MIGRATION_NAME = "20260719030000_pit_daily_candle_as_of_reader.sql"
CONTRACT_VERSION = "pit_daily_candle_as_of_reader.v1"
CURSOR_VERSION = "pit_daily_candle_as_of_cursor.v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

ENVELOPE_FIELDS = {
    "schema_version",
    "query_sha256",
    "snapshot_token",
    "snapshot_issued_at",
    "snapshot_manifest_sha256",
    "candidate_count",
    "items",
    "next_cursor",
}
CURSOR_FIELDS = {
    "schema_version",
    "query_sha256",
    "snapshot_token",
    "snapshot_issued_at",
    "snapshot_manifest_sha256",
    "last_session_date",
    "last_candle_observed_at",
    "last_evidence_available_at",
    "last_timing_revision",
    "last_timing_revision_id",
}
ITEM_FIELDS = {
    "timing_revision_id",
    "timing_idempotency_key",
    "timing_revision",
    "timing_canonical_evidence_sha256",
    "timing_evidence_available_at",
    "timing_received_at",
    "timing_payload",
    "candle_revision_id",
    "candle_revision",
    "candle_canonical_observation_sha256",
    "candle_revision_received_at",
    "candle_occurrence_id",
    "candle_occurrence_observed_at",
    "candle_occurrence_received_at",
    "candle_occurrence_origin",
    "candle_payload",
    "calendar_revision_id",
    "calendar_revision",
    "calendar_canonical_evidence_sha256",
    "calendar_revision_received_at",
    "calendar_occurrence_id",
    "calendar_occurrence_observed_at",
    "calendar_occurrence_received_at",
    "calendar_occurrence_origin",
    "calendar_payload",
    "candidate_lineage_sha256",
}
PIT_TABLES = (
    "pit_candle_stream_heads",
    "pit_candle_observation_revisions",
    "pit_candle_observation_occurrences",
    "pit_candle_observation_quarantine",
    "pit_calendar_stream_heads",
    "pit_calendar_content_revisions",
    "pit_calendar_observation_occurrences",
    "pit_calendar_observation_quarantine",
    "pit_daily_candle_timing_heads",
    "pit_daily_candle_timing_revisions",
    "pit_daily_candle_timing_quarantine",
    "pit_daily_candle_timing_request_ledger",
    "pit_daily_candle_timing_request_receipts",
)
CANDIDATE_FUNCTION = (
    "private.pit_daily_candle_as_of_candidates_v1("
    "text,text,text,text,boolean,date,date,timestamptz,pg_snapshot)"
)
PRIVATE_READER_FUNCTION = (
    "private.list_pit_daily_candles_as_of_v1_impl("
    "text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)"
)
WORKER_READER_FUNCTION = (
    "worker_api.list_pit_daily_candles_as_of_v1("
    "text,text,text,text,boolean,date,date,timestamptz,integer,jsonb)"
)


def canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"{field_name} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{field_name} is not a timestamp") from exc
    if parsed.tzinfo is None or canonical_timestamp(parsed) != value:
        raise VerificationError(f"{field_name} is not canonical UTC: {value}")
    return parsed


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def rpc_sql(
    *,
    provider: str,
    symbol: str,
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int,
    cursor: dict[str, object] | None = None,
) -> str:
    cursor_sql = (
        "null::jsonb"
        if cursor is None
        else jsonb_literal(cursor, "reader_cursor")
    )
    return jwt_claim_sql(WORKER_ID, role="service_role") + f"""
select worker_api.list_pit_daily_candles_as_of_v1(
  {sql_text(provider)},
  'KR',
  {sql_text(symbol)},
  '1d',
  true,
  {sql_text(start_session_date.isoformat())}::date,
  {sql_text(end_session_date.isoformat())}::date,
  {sql_text(as_of.isoformat())}::timestamptz,
  {limit},
  {cursor_sql}
)::text;
"""


def read_page(
    container: str,
    *,
    provider: str = "toss",
    symbol: str,
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int = 100,
    cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    rows = psql(
        container,
        rpc_sql(
            provider=provider,
            symbol=symbol,
            start_session_date=start_session_date,
            end_session_date=end_session_date,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
        ),
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("as-of reader returned no envelope")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict):
        raise VerificationError(f"as-of reader did not return an object: {parsed}")
    assert_envelope(parsed)
    return parsed


def assert_item(item: object) -> None:
    if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
        raise VerificationError(f"reader item shape mismatch: {item}")
    for field in (
        "timing_revision_id",
        "candle_revision_id",
        "candle_occurrence_id",
        "calendar_revision_id",
        "calendar_occurrence_id",
    ):
        try:
            UUID(str(item[field]))
        except (TypeError, ValueError) as exc:
            raise VerificationError(f"reader {field} is not a UUID") from exc
    for field in (
        "timing_idempotency_key",
        "timing_canonical_evidence_sha256",
        "candle_canonical_observation_sha256",
        "calendar_canonical_evidence_sha256",
        "candidate_lineage_sha256",
    ):
        if not _is_sha256(item[field]):
            raise VerificationError(f"reader {field} is not SHA-256")
    for field in (
        "timing_evidence_available_at",
        "timing_received_at",
        "candle_revision_received_at",
        "candle_occurrence_observed_at",
        "candle_occurrence_received_at",
        "calendar_revision_received_at",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_received_at",
    ):
        _parse_timestamp(item[field], field)
    if item["candle_occurrence_origin"] not in {
        "content_revision_backfill",
        "stream_head_recovery",
        "rpc",
    }:
        raise VerificationError("reader candle occurrence origin is invalid")
    if item["calendar_occurrence_origin"] not in {
        "content_revision_backfill",
        "stream_head_recovery",
        "rpc",
    }:
        raise VerificationError("reader calendar occurrence origin is invalid")
    candle = PointInTimeCandleV1.from_payload(item["candle_payload"])
    calendar = PointInTimeKrDailySessionV1.from_payload(item["calendar_payload"])
    timing = PointInTimeDailyCandleTimingEvidenceV1.from_payload(
        item["timing_payload"]
    )
    if build_daily_candle_timing_evidence(candle, calendar) != timing:
        raise VerificationError("reader item sources do not rebuild timing evidence")
    if candle.canonical_observation_sha256 != item[
        "candle_canonical_observation_sha256"
    ]:
        raise VerificationError("reader candle payload hash does not bind lineage")
    if calendar.canonical_evidence_sha256 != item[
        "calendar_canonical_evidence_sha256"
    ]:
        raise VerificationError("reader calendar payload hash does not bind lineage")
    if timing.canonical_timing_evidence_sha256 != item[
        "timing_canonical_evidence_sha256"
    ]:
        raise VerificationError("reader timing payload hash does not bind lineage")


def assert_envelope(envelope: dict[str, object]) -> None:
    if set(envelope) != ENVELOPE_FIELDS:
        raise VerificationError(f"reader envelope shape mismatch: {envelope}")
    if envelope["schema_version"] != CONTRACT_VERSION:
        raise VerificationError(f"reader schema version mismatch: {envelope}")
    for field in ("query_sha256", "snapshot_manifest_sha256"):
        if not _is_sha256(envelope[field]):
            raise VerificationError(f"reader {field} is not SHA-256")
    if not isinstance(envelope["snapshot_token"], str):
        raise VerificationError("reader snapshot token is not a string")
    _parse_timestamp(envelope["snapshot_issued_at"], "snapshot_issued_at")
    if (
        not isinstance(envelope["candidate_count"], int)
        or isinstance(envelope["candidate_count"], bool)
        or envelope["candidate_count"] < 0
    ):
        raise VerificationError("reader candidate count is invalid")
    items = envelope["items"]
    if not isinstance(items, list):
        raise VerificationError("reader items is not an array")
    for item in items:
        assert_item(item)
    cursor = envelope["next_cursor"]
    if cursor is not None:
        if not isinstance(cursor, dict) or set(cursor) != CURSOR_FIELDS:
            raise VerificationError(f"reader cursor shape mismatch: {cursor}")
        if cursor["schema_version"] != CURSOR_VERSION:
            raise VerificationError("reader cursor schema version mismatch")
        if cursor["query_sha256"] != envelope["query_sha256"]:
            raise VerificationError("reader cursor query binding mismatch")
        if cursor["snapshot_token"] != envelope["snapshot_token"]:
            raise VerificationError("reader cursor snapshot binding mismatch")
        if cursor["snapshot_issued_at"] != envelope["snapshot_issued_at"]:
            raise VerificationError("reader cursor issued-at binding mismatch")
        if (
            cursor["snapshot_manifest_sha256"]
            != envelope["snapshot_manifest_sha256"]
        ):
            raise VerificationError("reader cursor manifest binding mismatch")
        _parse_timestamp(cursor["last_candle_observed_at"], "cursor candle clock")
        _parse_timestamp(
            cursor["last_evidence_available_at"],
            "cursor evidence clock",
        )
        try:
            UUID(str(cursor["last_timing_revision_id"]))
        except (TypeError, ValueError) as exc:
            raise VerificationError("reader cursor UUID is invalid") from exc


def manifest_for(items: list[dict[str, object]]) -> str:
    raw = "\n".join(str(item["candidate_lineage_sha256"]) for item in items)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def drain_pages(
    container: str,
    *,
    symbol: str,
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int,
    first: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    pages: list[dict[str, object]] = []
    current = first or read_page(
        container,
        symbol=symbol,
        start_session_date=start_session_date,
        end_session_date=end_session_date,
        as_of=as_of,
        limit=limit,
    )
    while True:
        pages.append(current)
        cursor = current["next_cursor"]
        if cursor is None:
            break
        if not isinstance(cursor, dict):
            raise VerificationError("reader cursor changed type while draining")
        current = read_page(
            container,
            symbol=symbol,
            start_session_date=start_session_date,
            end_session_date=end_session_date,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
        )
        if len(pages) > 101:
            raise VerificationError("reader pagination did not terminate")
    items = [item for page in pages for item in page["items"]]
    if any(not isinstance(item, dict) for item in items):
        raise VerificationError("reader pagination returned a non-object item")
    return items, pages


def pit_fingerprint(container: str) -> str:
    parts = []
    for table in PIT_TABLES:
        parts.append(
            f"""select {sql_text(table)} as table_name,
md5(coalesce((select string_agg(to_jsonb(row_value)::text, E'\\n'
order by to_jsonb(row_value)::text) from private.{table} as row_value), ''))
as row_hash"""
        )
    query = " union all ".join(parts)
    return scalar(
        container,
        f"""
select md5(string_agg(table_name || ':' || row_hash, E'\\n' order by table_name))
from ({query}) as fingerprints;
""",
    )


def _append_fixture(
    container: str,
    *,
    day_offset: int,
    symbol: str,
    label: str,
    calendar_observed_minutes: int = 0,
) -> tuple[
    PointInTimeCandleV1,
    PointInTimeKrDailySessionV1,
    PointInTimeDailyCandleTimingEvidenceV1,
]:
    candle, calendar, timing = fixture(
        day_offset=day_offset,
        symbol=symbol,
        calendar_observed_minutes=calendar_observed_minutes,
    )
    append_candle_receipt(container, candle)
    receipt = append_timing(
        container,
        request_key(label),
        calendar,
        timing,
    )
    if receipt.get("status") != "stored":
        raise VerificationError(f"reader fixture was not stored: {receipt}")
    return candle, calendar, timing


def _oracle_selected_close(
    items: list[dict[str, object]],
    *,
    as_of: datetime,
) -> int:
    candidates = []
    for item in items:
        candle = PointInTimeCandleV1.from_payload(item["candle_payload"])
        timing = PointInTimeDailyCandleTimingEvidenceV1.from_payload(
            item["timing_payload"]
        )
        candidates.append((candle, timing))
    selected = select_daily_candles_as_of(candidates, as_of=as_of)
    if len(selected) != 1:
        raise VerificationError(f"Python oracle selected {len(selected)} candles")
    return selected[0].candle.close_krw


def verify_empty_envelope_and_acl(container: str) -> None:
    start = BASE_SESSION_DATE
    response = read_page(
        container,
        symbol="170000",
        start_session_date=start,
        end_session_date=start,
        as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )
    if (
        response["candidate_count"] != 0
        or response["items"] != []
        or response["next_cursor"] is not None
        or response["snapshot_manifest_sha256"] != EMPTY_SHA256
    ):
        raise VerificationError(f"empty reader envelope mismatch: {response}")

    non_iso_rows = psql(
        container,
        "set datestyle = 'SQL, DMY';\n"
        + rpc_sql(
            provider="toss",
            symbol="170000",
            start_session_date=start,
            end_session_date=start,
            as_of=datetime(2027, 1, 1, tzinfo=UTC),
            limit=100,
        ),
    ).stdout.strip().splitlines()
    if not non_iso_rows:
        raise VerificationError("non-ISO DateStyle reader returned no envelope")
    non_iso_response = json.loads(non_iso_rows[-1])
    if not isinstance(non_iso_response, dict):
        raise VerificationError("non-ISO DateStyle reader returned invalid JSON")
    assert_envelope(non_iso_response)
    if non_iso_response["query_sha256"] != response["query_sha256"]:
        raise VerificationError("reader query hash depends on session DateStyle")

    catalog = scalar(
        container,
        f"""
select concat_ws('|',
  (select p.prosecdef and p.provolatile='s' and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid={sql_text(CANDIDATE_FUNCTION)}::regprocedure),
  (select p.prosecdef and p.provolatile='s' and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid={sql_text(PRIVATE_READER_FUNCTION)}::regprocedure),
  (select not p.prosecdef and p.provolatile='s' and
          p.proconfig=array['search_path=""']::text[]
   from pg_catalog.pg_proc as p
   where p.oid={sql_text(WORKER_READER_FUNCTION)}::regprocedure),
  not pg_catalog.has_function_privilege('public',
    {sql_text(WORKER_READER_FUNCTION)}, 'EXECUTE'),
  pg_catalog.has_function_privilege('service_role',
    {sql_text(WORKER_READER_FUNCTION)}, 'EXECUTE'),
  not pg_catalog.has_function_privilege('service_role',
    {sql_text(CANDIDATE_FUNCTION)}, 'EXECUTE')
);
""",
    )
    if catalog != "t|t|t|t|t|t":
        raise VerificationError(f"reader catalog/ACL contract mismatch: {catalog}")

    base_call = """
select worker_api.list_pit_daily_candles_as_of_v1(
  'toss','KR','170000','1d',true,
  '2026-04-01'::date,'2026-04-01'::date,
  '2027-01-01T00:00:00Z'::timestamptz,100,null
);
"""
    for role in ("anon", "authenticated", "authenticator"):
        expect_failure(container, f"set role {role}; {base_call}", "permission denied")
    expect_failure(
        container,
        """
set role service_role;
select count(*) from private.pit_daily_candle_as_of_candidates_v1(
  'toss','KR','170000','1d',true,
  '2026-04-01'::date,'2026-04-01'::date,
  '2027-01-01T00:00:00Z'::timestamptz,pg_current_snapshot()
);
""",
        "permission denied",
    )
    print("PASS empty exact envelope, DateStyle independence and service-only ACL")


def verify_cutoff_and_timezone_equivalence(container: str) -> None:
    _, _, timing = _append_fixture(
        container,
        day_offset=20,
        symbol="170001",
        label="reader-cutoff",
    )
    session_date = timing.session_date
    before = read_page(
        container,
        symbol="170001",
        start_session_date=session_date,
        end_session_date=session_date,
        as_of=timing.evidence_available_at - timedelta(microseconds=1),
    )
    at_cutoff = read_page(
        container,
        symbol="170001",
        start_session_date=session_date,
        end_session_date=session_date,
        as_of=timing.evidence_available_at,
    )
    one_microsecond_later = read_page(
        container,
        symbol="170001",
        start_session_date=session_date,
        end_session_date=session_date,
        as_of=timing.evidence_available_at + timedelta(microseconds=1),
    )
    utc_equivalent = read_page(
        container,
        symbol="170001",
        start_session_date=session_date,
        end_session_date=session_date,
        as_of=timing.evidence_available_at.astimezone(UTC),
    )
    if before["candidate_count"] != 0 or before["items"]:
        raise VerificationError("reader included evidence one microsecond early")
    if len(at_cutoff["items"]) != 1 or len(one_microsecond_later["items"]) != 1:
        raise VerificationError("reader did not include exact cutoff evidence")
    if (
        at_cutoff["query_sha256"] != utc_equivalent["query_sha256"]
        or at_cutoff["snapshot_manifest_sha256"]
        != utc_equivalent["snapshot_manifest_sha256"]
        or at_cutoff["items"] != utc_equivalent["items"]
    ):
        raise VerificationError("KST/UTC equivalent instants changed reader result")
    print("PASS cutoff equality, +1us and KST/UTC instant equivalence")


def verify_revision_history_and_oracle(container: str) -> None:
    candle, calendar, timing = _append_fixture(
        container,
        day_offset=21,
        symbol="170002",
        label="reader-same-content-base",
    )
    later_same = reobserve_candle(
        candle,
        candle.observed_at + timedelta(minutes=1),
    )
    append_candle_receipt(container, later_same)
    later_same_timing = build_daily_candle_timing_evidence(later_same, calendar)
    append_timing(
        container,
        request_key("reader-same-content-later"),
        calendar,
        later_same_timing,
    )
    same_response = read_page(
        container,
        symbol="170002",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date,
        as_of=later_same_timing.evidence_available_at,
    )
    same_items = same_response["items"]
    if (
        same_response["candidate_count"] != 2
        or len(same_items) != 2
        or len({item["candle_occurrence_id"] for item in same_items}) != 2
        or len(
            {item["candle_canonical_observation_sha256"] for item in same_items}
        )
        != 1
        or _oracle_selected_close(
            same_items,
            as_of=later_same_timing.evidence_available_at,
        )
        != candle.close_krw
    ):
        raise VerificationError("same-content later occurrence history mismatch")

    original, correction_calendar, original_timing = _append_fixture(
        container,
        day_offset=22,
        symbol="170003",
        label="reader-correction-base",
    )
    corrected = reobserve_candle(
        original,
        original.observed_at + timedelta(minutes=1),
        close_krw=original.close_krw + 900,
    )
    append_candle_receipt(container, corrected)
    corrected_timing = build_daily_candle_timing_evidence(
        corrected,
        correction_calendar,
    )
    append_timing(
        container,
        request_key("reader-correction-b"),
        correction_calendar,
        corrected_timing,
    )
    correction_response = read_page(
        container,
        symbol="170003",
        start_session_date=original_timing.session_date,
        end_session_date=original_timing.session_date,
        as_of=corrected_timing.evidence_available_at,
    )
    correction_items = correction_response["items"]
    if (
        correction_response["candidate_count"] != 2
        or _oracle_selected_close(
            correction_items,
            as_of=corrected_timing.evidence_available_at,
        )
        != corrected.close_krw
    ):
        raise VerificationError("A-to-B correction did not match Python selector")

    recurrent = reobserve_candle(
        original,
        original.observed_at + timedelta(minutes=2),
    )
    quarantine = append_candle_receipt(container, recurrent)
    if (
        quarantine.get("status") != "quarantined"
        or quarantine.get("reason_code")
        != "candle_observation_store_historical_hash_recurrence_ambiguous"
    ):
        raise VerificationError(f"A-B-A recurrence was not quarantined: {quarantine}")
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol="170003",
            start_session_date=original_timing.session_date,
            end_session_date=original_timing.session_date,
            as_of=recurrent.observed_at,
            limit=100,
        ),
        "pit_daily_candle_as_of_reader_timeline_ambiguous",
    )
    print("PASS raw replay/correction history, Python oracle and A-B-A fail-close")


def verify_same_availability_component_advance(container: str) -> None:
    candle, calendar, timing = _append_fixture(
        container,
        day_offset=23,
        symbol="170004",
        label="reader-same-availability-base",
        calendar_observed_minutes=3,
    )
    later_candle = reobserve_candle(
        candle,
        candle.observed_at + timedelta(minutes=1),
    )
    append_candle_receipt(container, later_candle)
    later_timing = build_daily_candle_timing_evidence(later_candle, calendar)
    if later_timing.evidence_available_at != timing.evidence_available_at:
        raise VerificationError("same-availability fixture changed max clock")
    append_timing(
        container,
        request_key("reader-same-availability-later"),
        calendar,
        later_timing,
    )
    response = read_page(
        container,
        symbol="170004",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date,
        as_of=timing.evidence_available_at,
    )
    items = response["items"]
    if (
        response["candidate_count"] != 2
        or [item["timing_revision"] for item in items] != [1, 2]
        or len({item["timing_evidence_available_at"] for item in items}) != 1
        or len({item["candle_occurrence_observed_at"] for item in items}) != 2
    ):
        raise VerificationError(f"same-max component advance mismatch: {response}")
    print("PASS same max availability preserves advancing source component")


def verify_pagination_cursor_and_snapshot(container: str) -> None:
    symbol = "170050"
    fixtures = [
        _append_fixture(
            container,
            day_offset=day_offset,
            symbol=symbol,
            label=f"reader-page-{day_offset}",
        )
        for day_offset in range(30, 56)
    ]
    start = fixtures[0][2].session_date
    end = BASE_SESSION_DATE + timedelta(days=57)
    as_of = fixtures[-1][2].evidence_available_at + timedelta(days=10)
    first = read_page(
        container,
        symbol=symbol,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=25,
    )
    cursor = first["next_cursor"]
    if not isinstance(cursor, dict):
        raise VerificationError("pagination fixture did not create a cursor")

    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol=symbol,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor={"malformed": True},
        ),
        "pit_daily_candle_as_of_reader_cursor_invalid",
    )
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol=symbol,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=26,
            cursor=cursor,
        ),
        "pit_daily_candle_as_of_reader_cursor_invalid",
    )
    expired = dict(cursor)
    expired["snapshot_issued_at"] = "2000-01-01T00:00:00Z"
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol=symbol,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=expired,
        ),
        "pit_daily_candle_as_of_reader_cursor_invalid",
    )
    outside = dict(cursor)
    outside["last_session_date"] = "1990-01-01"
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol=symbol,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=outside,
        ),
        "pit_daily_candle_as_of_reader_cursor_invalid",
    )
    wrong_manifest = dict(cursor)
    wrong_manifest["snapshot_manifest_sha256"] = "f" * 64
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol=symbol,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=wrong_manifest,
        ),
        "pit_daily_candle_as_of_reader_snapshot_mismatch",
    )

    _append_fixture(
        container,
        day_offset=56,
        symbol=symbol,
        label="reader-page-later-commit",
    )
    snapshot_items, pages = drain_pages(
        container,
        symbol=symbol,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=25,
        first=first,
    )
    ids = [item["timing_revision_id"] for item in snapshot_items]
    snapshot_tokens = {page["snapshot_token"] for page in pages}
    manifests = {page["snapshot_manifest_sha256"] for page in pages}
    counts = {page["candidate_count"] for page in pages}
    if (
        len(snapshot_items) != 26
        or len(ids) != len(set(ids))
        or len(pages) < 2
        or len(snapshot_tokens) != 1
        or len(manifests) != 1
        or counts != {26}
        or manifest_for(snapshot_items) != first["snapshot_manifest_sha256"]
    ):
        raise VerificationError("snapshot pagination had a duplicate, gap or drift")

    current = read_page(
        container,
        symbol=symbol,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=100,
    )
    repeated = read_page(
        container,
        symbol=symbol,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=100,
    )
    if (
        current["candidate_count"] != 27
        or len(current["items"]) != 27
        or manifest_for(current["items"])
        != current["snapshot_manifest_sha256"]
        or current["items"] != repeated["items"]
        or current["snapshot_manifest_sha256"]
        != repeated["snapshot_manifest_sha256"]
    ):
        raise VerificationError("fresh snapshot did not deterministically include commit")
    print("PASS cursor rejection, keyset pagination, manifest and MVCC snapshot")


def verify_exact_occurrence_lineage(container: str) -> None:
    _, _, timing = _append_fixture(
        container,
        day_offset=40,
        symbol="170060",
        label="reader-exact-lineage",
    )
    response = read_page(
        container,
        symbol="170060",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date,
        as_of=timing.evidence_available_at,
    )
    items = response["items"]
    if len(items) != 1:
        raise VerificationError("exact lineage fixture did not return one row")
    item = items[0]
    exact = scalar(
        container,
        f"""
select count(*)
from private.pit_daily_candle_timing_revisions as timing
join private.pit_candle_observation_occurrences as candle_occurrence
  on candle_occurrence.id=timing.candle_occurrence_id
 and candle_occurrence.content_revision_id=timing.candle_revision_id
join private.pit_candle_observation_revisions as candle_revision
  on candle_revision.id=candle_occurrence.content_revision_id
 and candle_revision.idempotency_key=candle_occurrence.idempotency_key
 and candle_revision.canonical_observation_sha256=
     candle_occurrence.canonical_observation_sha256
join private.pit_calendar_observation_occurrences as calendar_occurrence
  on calendar_occurrence.id=timing.calendar_occurrence_id
 and calendar_occurrence.content_revision_id=timing.calendar_revision_id
join private.pit_calendar_content_revisions as calendar_revision
  on calendar_revision.id=calendar_occurrence.content_revision_id
 and calendar_revision.calendar_idempotency_key=
     calendar_occurrence.calendar_idempotency_key
 and calendar_revision.canonical_evidence_sha256=
     calendar_occurrence.canonical_evidence_sha256
where timing.id={sql_text(str(item['timing_revision_id']))}::uuid
  and candle_revision.id={sql_text(str(item['candle_revision_id']))}::uuid
  and candle_occurrence.id={sql_text(str(item['candle_occurrence_id']))}::uuid
  and calendar_revision.id={sql_text(str(item['calendar_revision_id']))}::uuid
  and calendar_occurrence.id={sql_text(str(item['calendar_occurrence_id']))}::uuid
  and candle_occurrence.observation_payload=
      {jsonb_literal(item['candle_payload'], 'reader_candle_lineage')}
  and calendar_occurrence.observation_payload=
      {jsonb_literal(item['calendar_payload'], 'reader_calendar_lineage')}
  and timing.timing_payload=
      {jsonb_literal(item['timing_payload'], 'reader_timing_lineage')};
""",
    )
    if exact != "1":
        raise VerificationError("reader item did not bind exact composite lineage")
    print("PASS exact timing-to-occurrence-to-content composite lineage")


def verify_zero_write_contract(container: str) -> None:
    before = pit_fingerprint(container)
    session_date = BASE_SESSION_DATE + timedelta(days=40)
    read_page(
        container,
        symbol="170060",
        start_session_date=session_date,
        end_session_date=session_date,
        as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )
    after_success = pit_fingerprint(container)
    if before != after_success:
        raise VerificationError("successful reader call changed PIT domain rows")
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol="170060",
            start_session_date=session_date,
            end_session_date=session_date,
            as_of=datetime(2027, 1, 1, tzinfo=UTC),
            limit=24,
        ),
        "pit_daily_candle_as_of_reader_argument_invalid",
    )
    expect_failure(
        container,
        rpc_sql(
            provider="toss",
            symbol="170060",
            start_session_date=session_date,
            end_session_date=session_date,
            as_of=datetime(2027, 1, 1, tzinfo=UTC),
            limit=101,
        ),
        "pit_daily_candle_as_of_reader_argument_invalid",
    )
    after_error = pit_fingerprint(container)
    if before != after_error:
        raise VerificationError("rejected reader call changed PIT domain rows")
    print("PASS successful and rejected reads perform zero domain writes")


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("PIT as-of reader migration is missing") from exc

    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    _, _, timing = _append_fixture(
        container,
        day_offset=70,
        symbol="180001",
        label="reader-upgrade-existing",
    )
    pit_before = pit_fingerprint(container)
    settings_before = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings
where settings.id='singleton';
""",
    )
    if not settings_before:
        raise VerificationError("populated reader upgrade lacks bot settings")

    psql(container, target.read_text(encoding="utf-8"))
    if pit_fingerprint(container) != pit_before:
        raise VerificationError("reader migration changed populated PIT rows")
    initial = read_page(
        container,
        symbol="180001",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date + timedelta(days=1),
        as_of=timing.evidence_available_at + timedelta(days=10),
    )
    if initial["candidate_count"] != 1 or len(initial["items"]) != 1:
        raise VerificationError("reader could not read populated pre-target row")

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
        raise VerificationError("reader/future migration changed bot settings")
    converged = read_page(
        container,
        symbol="180001",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date + timedelta(days=1),
        as_of=timing.evidence_available_at + timedelta(days=10),
    )
    if (
        converged["items"] != initial["items"]
        or converged["snapshot_manifest_sha256"]
        != initial["snapshot_manifest_sha256"]
    ):
        raise VerificationError("future migration changed existing reader result")

    _append_fixture(
        container,
        day_offset=71,
        symbol="180001",
        label="reader-upgrade-future-write",
    )
    after_write = read_page(
        container,
        symbol="180001",
        start_session_date=timing.session_date,
        end_session_date=timing.session_date + timedelta(days=1),
        as_of=timing.evidence_available_at + timedelta(days=10),
    )
    if after_write["candidate_count"] != 2 or len(after_write["items"]) != 2:
        raise VerificationError("post-convergence write/read did not succeed")
    print("PASS populated counts/hash, target upgrade and future convergence")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-reader-fresh-{suffix}"
    upgrade = f"msp-pit-reader-upgrade-{suffix}"
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
        verify_empty_envelope_and_acl(fresh)
        verify_cutoff_and_timezone_equivalence(fresh)
        verify_revision_history_and_oracle(fresh)
        verify_same_availability_component_advance(fresh)
        verify_pagination_cursor_and_snapshot(fresh)
        verify_exact_occurrence_lineage(fresh)
        verify_zero_write_contract(fresh)
        verify_populated_upgrade(upgrade)
        print("FINAL=PASS pit_daily_candle_as_of_reader_verifier")
        return 0
    except (
        VerificationError,
        KeyError,
        OSError,
        TypeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
