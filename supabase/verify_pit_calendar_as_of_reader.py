#!/usr/bin/env python3
"""Verify the bounded durable PIT KR calendar reader in disposable PostgreSQL.

The verifier never connects to a hosted project. It exercises fresh and
populated upgrades, immutable occurrence lineage, semantic as-of selection,
fixed-snapshot pagination, ambiguity quarantine, capacity bounds, ACL/RLS,
and zero-write behavior on PostgreSQL 17.
"""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import cast
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
from verify_pit_calendar_observation_store import (  # noqa: E402
    append_calendar,
)
from verify_pit_daily_candle_timing_store import (  # noqa: E402
    WORKER_ID,
    jsonb_literal,
    scalar,
    sql_text,
)

from app.application.ports.calendar_as_of_reader_port import (  # noqa: E402
    CalendarAsOfReadRequest,
    calendar_as_of_query_sha256,
)
from app.domain.common.time import KST  # noqa: E402
from app.domain.market_data.calendar_as_of import (  # noqa: E402
    select_kr_daily_sessions_as_of,
)
from app.domain.market_data.point_in_time_calendar import (  # noqa: E402
    PointInTimeKrDailySessionV1,
)

MIGRATION_NAME = "20260719050000_pit_calendar_as_of_reader.sql"
CONTRACT_VERSION = "pit_calendar_as_of_reader.v1"
CURSOR_VERSION = "pit_calendar_as_of_cursor.v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
BASE_SESSION_DATE = date(2025, 1, 1)
BASE_OBSERVED_AT = datetime(2024, 12, 1, tzinfo=UTC)
CONTRACT_SHA256 = "d" * 64

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
    "last_occurrence_observed_at",
    "last_calendar_revision",
    "last_calendar_revision_id",
    "last_calendar_occurrence_id",
}
ITEM_FIELDS = {
    "session_date",
    "calendar_idempotency_key",
    "calendar_revision_id",
    "calendar_revision",
    "calendar_canonical_evidence_sha256",
    "calendar_revision_observed_at",
    "calendar_revision_received_at",
    "calendar_content_payload",
    "calendar_occurrence_id",
    "calendar_occurrence_observed_at",
    "calendar_occurrence_received_at",
    "calendar_occurrence_origin",
    "calendar_payload",
    "candidate_lineage_sha256",
}
LINEAGE_FIELDS = (
    "calendar_revision_id",
    "calendar_idempotency_key",
    "calendar_revision",
    "calendar_canonical_evidence_sha256",
    "calendar_revision_observed_at",
    "calendar_revision_received_at",
    "calendar_occurrence_id",
    "calendar_occurrence_observed_at",
    "calendar_occurrence_received_at",
    "calendar_occurrence_origin",
)
PRIVATE_TABLES = (
    "pit_calendar_stream_heads",
    "pit_calendar_content_revisions",
    "pit_calendar_observation_occurrences",
    "pit_calendar_observation_quarantine",
)
ZERO_WRITE_TABLES = (
    ("public", "orders"),
    ("public", "positions"),
    ("public", "features_daily"),
    ("public", "decision_snapshots"),
    ("private", "order_intents"),
    ("private", "order_attempts"),
    ("private", "order_events"),
    ("private", "order_reservations"),
    ("private", "fills"),
    ("private", "execution_decisions"),
    ("private", "execution_observations"),
    ("private", "pit_calendar_stream_heads"),
    ("private", "pit_calendar_content_revisions"),
    ("private", "pit_calendar_observation_occurrences"),
    ("private", "pit_calendar_observation_quarantine"),
)
CANDIDATE_FUNCTION = (
    "private.pit_kr_calendar_as_of_candidates_v1("
    "text,text,date,date,timestamptz,pg_snapshot)"
)
CALENDAR_IDENTITY_FUNCTION = (
    "private.pit_calendar_identity_sha256_v1(text,text,date)"
)
CALENDAR_EVIDENCE_FUNCTION = (
    "private.pit_calendar_canonical_evidence_sha256_v1("
    "text,text,date,boolean,timestamptz,timestamptz,date,timestamptz,"
    "timestamptz,text)"
)
PRIVATE_READER_FUNCTION = (
    "private.list_pit_kr_daily_sessions_as_of_v1_impl("
    "text,text,date,date,timestamptz,integer,jsonb)"
)
WORKER_READER_FUNCTION = (
    "worker_api.list_pit_kr_daily_sessions_as_of_v1("
    "text,text,date,date,timestamptz,integer,jsonb)"
)


def canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"{field_name} is not timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{field_name} is not a timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or canonical_timestamp(parsed) != value
    ):
        raise VerificationError(f"{field_name} is not canonical UTC: {value}")
    return parsed.astimezone(UTC)


def is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def calendar_fixture(
    *,
    day_offset: int,
    is_open: bool = True,
    observed_minutes: int = 0,
    regular_end_delta_minutes: int = 0,
) -> PointInTimeKrDailySessionV1:
    session_date = BASE_SESSION_DATE + timedelta(days=day_offset)
    next_business_date = session_date + timedelta(days=1)
    regular_start_at = (
        datetime.combine(session_date, time(9), tzinfo=KST)
        if is_open
        else None
    )
    regular_end_at = (
        datetime.combine(session_date, time(15, 30), tzinfo=KST)
        + timedelta(minutes=regular_end_delta_minutes)
        if is_open
        else None
    )
    return PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=regular_start_at,
        regular_end_at=regular_end_at,
        next_business_date=next_business_date,
        next_regular_start_at=datetime.combine(
            next_business_date,
            time(9),
            tzinfo=KST,
        ),
        next_regular_end_at=datetime.combine(
            next_business_date,
            time(15, 30),
            tzinfo=KST,
        ),
        observed_at=(
            BASE_OBSERVED_AT
            + timedelta(days=day_offset, minutes=observed_minutes)
        ),
        provider_contract_sha256=CONTRACT_SHA256,
    )


def reobserve(
    source: PointInTimeKrDailySessionV1,
    *,
    minutes: int,
    regular_end_delta_minutes: int = 0,
) -> PointInTimeKrDailySessionV1:
    regular_end_at = source.regular_end_at
    if regular_end_at is not None:
        regular_end_at += timedelta(minutes=regular_end_delta_minutes)
    return PointInTimeKrDailySessionV1.create(
        provider=source.provider,
        market=source.market,
        session_date=source.session_date,
        is_open=source.is_open,
        regular_start_at=source.regular_start_at,
        regular_end_at=regular_end_at,
        next_business_date=source.next_business_date,
        next_regular_start_at=source.next_regular_start_at,
        next_regular_end_at=source.next_regular_end_at,
        observed_at=source.observed_at + timedelta(minutes=minutes),
        provider_contract_sha256=source.provider_contract_sha256,
    )


def rpc_sql(
    *,
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int = 100,
    cursor: dict[str, object] | None = None,
    provider: str = "toss",
    market: str = "KR",
) -> str:
    cursor_sql = (
        "null::jsonb"
        if cursor is None
        else jsonb_literal(cursor, "calendar_reader_cursor")
    )
    return jwt_claim_sql(WORKER_ID, role="service_role") + f"""
select worker_api.list_pit_kr_daily_sessions_as_of_v1(
  {sql_text(provider)},
  {sql_text(market)},
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
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int = 100,
    cursor: dict[str, object] | None = None,
    provider: str = "toss",
) -> dict[str, object]:
    rows = psql(
        container,
        rpc_sql(
            provider=provider,
            start_session_date=start_session_date,
            end_session_date=end_session_date,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
        ),
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("calendar as-of reader returned no envelope")
    parsed = json.loads(rows[-1])
    if not isinstance(parsed, dict):
        raise VerificationError("calendar as-of reader returned non-object JSON")
    assert_envelope(parsed)
    expected_query = calendar_as_of_query_sha256(
        CalendarAsOfReadRequest(
            provider=provider,
            market="KR",
            start_session_date=start_session_date,
            end_session_date=end_session_date,
            as_of=as_of,
            page_size=limit,
        )
    )
    if parsed["query_sha256"] != expected_query:
        raise VerificationError("Python/SQL query SHA-256 vector mismatch")
    return parsed


def assert_item(item: object, snapshot_issued_at: datetime) -> None:
    if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
        raise VerificationError(f"calendar reader item shape mismatch: {item}")
    for field in ("calendar_revision_id", "calendar_occurrence_id"):
        try:
            parsed_uuid = UUID(str(item[field]))
        except (TypeError, ValueError) as exc:
            raise VerificationError(f"calendar reader {field} is not UUID") from exc
        if str(parsed_uuid) != item[field]:
            raise VerificationError(f"calendar reader {field} is not canonical UUID")
    for field in (
        "calendar_idempotency_key",
        "calendar_canonical_evidence_sha256",
        "candidate_lineage_sha256",
    ):
        if not is_sha256(item[field]):
            raise VerificationError(f"calendar reader {field} is not SHA-256")
    if (
        not isinstance(item["calendar_revision"], int)
        or isinstance(item["calendar_revision"], bool)
        or item["calendar_revision"] <= 0
    ):
        raise VerificationError("calendar reader revision is invalid")
    timestamps = {
        field: parse_timestamp(item[field], field)
        for field in (
            "calendar_revision_observed_at",
            "calendar_revision_received_at",
            "calendar_occurrence_observed_at",
            "calendar_occurrence_received_at",
        )
    }
    if (
        timestamps["calendar_revision_observed_at"]
        > timestamps["calendar_occurrence_observed_at"]
        or timestamps["calendar_revision_observed_at"]
        > timestamps["calendar_revision_received_at"]
        or timestamps["calendar_revision_received_at"]
        > timestamps["calendar_occurrence_received_at"]
        or timestamps["calendar_occurrence_observed_at"]
        > timestamps["calendar_occurrence_received_at"]
        or timestamps["calendar_occurrence_received_at"] > snapshot_issued_at
    ):
        raise VerificationError("calendar reader lineage clock order is invalid")
    if item["calendar_occurrence_origin"] not in {
        "content_revision_backfill",
        "stream_head_recovery",
        "rpc",
    }:
        raise VerificationError("calendar reader occurrence origin is invalid")

    content_payload = item["calendar_content_payload"]
    occurrence_payload = item["calendar_payload"]
    if not isinstance(content_payload, dict) or not isinstance(
        occurrence_payload, dict
    ):
        raise VerificationError("calendar reader payload is not an object")
    content = PointInTimeKrDailySessionV1.from_payload(content_payload)
    occurrence = PointInTimeKrDailySessionV1.from_payload(occurrence_payload)
    content_semantics = dict(content_payload)
    occurrence_semantics = dict(occurrence_payload)
    content_semantics.pop("observed_at")
    occurrence_semantics.pop("observed_at")
    if content_semantics != occurrence_semantics:
        raise VerificationError("calendar content/occurrence semantics mismatch")
    if (
        occurrence.session_date.isoformat() != item["session_date"]
        or occurrence.idempotency_key != item["calendar_idempotency_key"]
        or content.idempotency_key != item["calendar_idempotency_key"]
        or occurrence.canonical_evidence_sha256
        != item["calendar_canonical_evidence_sha256"]
        or content.canonical_evidence_sha256
        != item["calendar_canonical_evidence_sha256"]
        or content.observed_at.astimezone(UTC)
        != timestamps["calendar_revision_observed_at"]
        or occurrence.observed_at.astimezone(UTC)
        != timestamps["calendar_occurrence_observed_at"]
    ):
        raise VerificationError("calendar reader payload lineage mismatch")

    canonical_line = "|".join(str(item[field]) for field in LINEAGE_FIELDS)
    expected_lineage = hashlib.sha256(canonical_line.encode("utf-8")).hexdigest()
    if item["candidate_lineage_sha256"] != expected_lineage:
        raise VerificationError("Python/SQL lineage SHA-256 vector mismatch")


def assert_envelope(envelope: dict[str, object]) -> None:
    if set(envelope) != ENVELOPE_FIELDS:
        raise VerificationError(f"calendar reader envelope shape mismatch: {envelope}")
    if envelope["schema_version"] != CONTRACT_VERSION:
        raise VerificationError("calendar reader schema version mismatch")
    for field in ("query_sha256", "snapshot_manifest_sha256"):
        if not is_sha256(envelope[field]):
            raise VerificationError(f"calendar reader {field} is not SHA-256")
    if not isinstance(envelope["snapshot_token"], str):
        raise VerificationError("calendar reader snapshot token is not text")
    snapshot_issued_at = parse_timestamp(
        envelope["snapshot_issued_at"],
        "snapshot_issued_at",
    )
    candidate_count = envelope["candidate_count"]
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or not 0 <= candidate_count <= 1_000
    ):
        raise VerificationError("calendar reader candidate count is invalid")
    items = envelope["items"]
    if not isinstance(items, list):
        raise VerificationError("calendar reader items is not an array")
    for item in items:
        assert_item(item, snapshot_issued_at)
    if len(items) > candidate_count:
        raise VerificationError("calendar reader page exceeds candidate count")
    item_objects = cast(list[dict[str, object]], items)
    order_keys = [item_order_key(item) for item in item_objects]
    if order_keys != sorted(order_keys) or len(order_keys) != len(set(order_keys)):
        raise VerificationError("calendar reader page order is not strict")
    cursor = envelope["next_cursor"]
    if cursor is not None:
        if not isinstance(cursor, dict) or set(cursor) != CURSOR_FIELDS:
            raise VerificationError("calendar reader cursor shape mismatch")
        if (
            cursor["schema_version"] != CURSOR_VERSION
            or cursor["query_sha256"] != envelope["query_sha256"]
            or cursor["snapshot_token"] != envelope["snapshot_token"]
            or cursor["snapshot_issued_at"] != envelope["snapshot_issued_at"]
            or cursor["snapshot_manifest_sha256"]
            != envelope["snapshot_manifest_sha256"]
        ):
            raise VerificationError("calendar reader cursor metadata mismatch")
        parse_timestamp(
            cursor["last_occurrence_observed_at"],
            "cursor occurrence clock",
        )
        for field in (
            "last_calendar_revision_id",
            "last_calendar_occurrence_id",
        ):
            try:
                UUID(str(cursor[field]))
            except (TypeError, ValueError) as exc:
                raise VerificationError("calendar reader cursor UUID invalid") from exc
        if not item_objects:
            raise VerificationError("calendar reader cursor followed an empty page")
        last_item = item_objects[-1]
        if (
            cursor["last_session_date"] != last_item["session_date"]
            or cursor["last_occurrence_observed_at"]
            != last_item["calendar_occurrence_observed_at"]
            or cursor["last_calendar_revision"]
            != last_item["calendar_revision"]
            or cursor["last_calendar_revision_id"]
            != last_item["calendar_revision_id"]
            or cursor["last_calendar_occurrence_id"]
            != last_item["calendar_occurrence_id"]
        ):
            raise VerificationError("calendar reader cursor position mismatch")


def item_order_key(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item["session_date"],
        item["calendar_occurrence_observed_at"],
        item["calendar_revision"],
        item["calendar_revision_id"],
        item["calendar_occurrence_id"],
    )


def manifest_for(items: list[dict[str, object]]) -> str:
    joined = "\n".join(str(item["candidate_lineage_sha256"]) for item in items)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def drain_pages(
    container: str,
    *,
    start_session_date: date,
    end_session_date: date,
    as_of: datetime,
    limit: int,
    first: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    pages: list[dict[str, object]] = []
    current = first or read_page(
        container,
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
            raise VerificationError("calendar reader cursor changed type")
        current = read_page(
            container,
            start_session_date=start_session_date,
            end_session_date=end_session_date,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
        )
        if len(pages) > 41:
            raise VerificationError("calendar reader pagination did not terminate")
    raw_items = [item for page in pages for item in page["items"]]
    if any(not isinstance(item, dict) for item in raw_items):
        raise VerificationError("calendar reader returned non-object item")
    typed_items = cast(list[dict[str, object]], raw_items)
    order_keys = [item_order_key(item) for item in typed_items]
    if order_keys != sorted(order_keys) or len(order_keys) != len(set(order_keys)):
        raise VerificationError("calendar reader cross-page order is not strict")
    return typed_items, pages


def selected_sessions(
    items: list[dict[str, object]],
    *,
    as_of: datetime,
) -> tuple[PointInTimeKrDailySessionV1, ...]:
    selected = select_kr_daily_sessions_as_of(
        [
            PointInTimeKrDailySessionV1.from_payload(item["calendar_payload"])
            for item in items
        ],
        as_of=as_of,
    )
    return tuple(item.session for item in selected)


def table_fingerprint(container: str) -> str:
    fragments = []
    for schema, table in ZERO_WRITE_TABLES:
        label = f"{schema}.{table}"
        fragments.append(
            f"""select {sql_text(label)} as table_name,
md5(coalesce((select string_agg(to_jsonb(row_value)::text, E'\\n'
order by to_jsonb(row_value)::text) from {schema}.{table} as row_value), ''))
as row_hash"""
        )
    return scalar(
        container,
        "select md5(string_agg(table_name || ':' || row_hash, E'\\n' "
        "order by table_name)) from ("
        + " union all ".join(fragments)
        + ") as fingerprints;",
    )


def verify_empty_open_closed_and_hash_vectors(container: str) -> None:
    empty_date = BASE_SESSION_DATE + timedelta(days=300)
    empty = read_page(
        container,
        start_session_date=empty_date,
        end_session_date=empty_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if (
        empty["candidate_count"] != 0
        or empty["items"] != []
        or empty["next_cursor"] is not None
        or empty["snapshot_manifest_sha256"] != EMPTY_SHA256
    ):
        raise VerificationError(f"empty calendar envelope mismatch: {empty}")

    open_session = calendar_fixture(day_offset=0, is_open=True)
    closed_session = calendar_fixture(day_offset=1, is_open=False)
    append_calendar(container, open_session)
    append_calendar(container, closed_session)
    response = read_page(
        container,
        start_session_date=open_session.session_date,
        end_session_date=closed_session.session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    items = cast(list[dict[str, object]], response["items"])
    selected = selected_sessions(items, as_of=datetime(2026, 1, 1, tzinfo=UTC))
    if (
        response["candidate_count"] != 2
        or len(items) != 2
        or [item.is_open for item in selected] != [True, False]
        or items[0]["calendar_content_payload"] != items[0]["calendar_payload"]
        or manifest_for(items) != response["snapshot_manifest_sha256"]
    ):
        raise VerificationError("open/closed calendar result or hash vector mismatch")

    non_iso_rows = psql(
        container,
        "set datestyle = 'SQL, DMY';\n"
        + rpc_sql(
            start_session_date=open_session.session_date,
            end_session_date=closed_session.session_date,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ).stdout.strip().splitlines()
    if not non_iso_rows:
        raise VerificationError("DateStyle-independent vector returned no row")
    non_iso = json.loads(non_iso_rows[-1])
    if not isinstance(non_iso, dict):
        raise VerificationError("DateStyle-independent vector returned non-object")
    assert_envelope(non_iso)
    if (
        non_iso["query_sha256"] != response["query_sha256"]
        or non_iso["candidate_count"] != response["candidate_count"]
        or non_iso["items"] != response["items"]
        or non_iso["snapshot_manifest_sha256"]
        != response["snapshot_manifest_sha256"]
        or non_iso["next_cursor"] != response["next_cursor"]
    ):
        raise VerificationError("calendar result or hash depends on DateStyle")
    print("PASS Python/SQL hashes, empty, open and closed calendar envelopes")


def verify_replay_correction_and_cutoff(container: str) -> None:
    initial = calendar_fixture(day_offset=10)
    exact_first = append_calendar(container, initial)
    exact_replay = append_calendar(container, initial)
    if (
        exact_first.get("status") != "stored"
        or exact_replay.get("status") != "replayed"
        or exact_replay.get("occurrence_inserted") is not False
        or exact_replay.get("occurrence_id") != exact_first.get("occurrence_id")
    ):
        raise VerificationError("exact calendar replay was not idempotent")

    later_same = reobserve(initial, minutes=1)
    later_receipt = append_calendar(container, later_same)
    corrected = reobserve(
        initial,
        minutes=2,
        regular_end_delta_minutes=1,
    )
    correction_receipt = append_calendar(container, corrected)
    if (
        later_receipt.get("status") != "replayed"
        or later_receipt.get("occurrence_inserted") is not True
        or correction_receipt.get("status") != "stored"
        or correction_receipt.get("revision") != 2
    ):
        raise VerificationError("later unchanged/correction receipts mismatch")

    at_initial = read_page(
        container,
        start_session_date=initial.session_date,
        end_session_date=initial.session_date,
        as_of=initial.observed_at,
    )
    before_correction = read_page(
        container,
        start_session_date=initial.session_date,
        end_session_date=initial.session_date,
        as_of=corrected.observed_at - timedelta(microseconds=1),
    )
    at_correction = read_page(
        container,
        start_session_date=initial.session_date,
        end_session_date=initial.session_date,
        as_of=corrected.observed_at,
    )
    before_items = cast(list[dict[str, object]], before_correction["items"])
    corrected_items = cast(list[dict[str, object]], at_correction["items"])
    before_selected = selected_sessions(
        before_items,
        as_of=corrected.observed_at - timedelta(microseconds=1),
    )
    corrected_selected = selected_sessions(
        corrected_items,
        as_of=corrected.observed_at,
    )
    if (
        at_initial["candidate_count"] != 1
        or before_correction["candidate_count"] != 2
        or at_correction["candidate_count"] != 3
        or before_selected != (later_same,)
        or corrected_selected != (corrected,)
        or corrected_items[1]["calendar_content_payload"]
        == corrected_items[1]["calendar_payload"]
        or corrected_items[1]["calendar_revision"] != 1
        or corrected_items[2]["calendar_revision"] != 2
    ):
        raise VerificationError("calendar correction cutoff/raw history mismatch")
    print("PASS exact replay, later unchanged occurrence and correction cutoff")


def verify_quarantine_blocks_complete_timeline(container: str) -> None:
    recurrent_base = calendar_fixture(day_offset=20)
    recurrent_b = reobserve(
        recurrent_base,
        minutes=2,
        regular_end_delta_minutes=1,
    )
    recurrent_a = reobserve(recurrent_base, minutes=3)
    append_calendar(container, recurrent_base)
    append_calendar(container, recurrent_b)
    recurrent_receipt = append_calendar(container, recurrent_a)
    if recurrent_receipt.get("reason_code") != (
        "pit_calendar_historical_hash_recurrence_ambiguous"
    ):
        raise VerificationError("A-B-A calendar recurrence was not quarantined")
    before_future_quarantine = read_page(
        container,
        start_session_date=recurrent_base.session_date,
        end_session_date=recurrent_base.session_date,
        as_of=recurrent_b.observed_at,
    )
    before_future_items = cast(
        list[dict[str, object]],
        before_future_quarantine["items"],
    )
    if (
        before_future_quarantine["candidate_count"] != 2
        or selected_sessions(
            before_future_items,
            as_of=recurrent_b.observed_at,
        )
        != (recurrent_b,)
    ):
        raise VerificationError("future quarantine polluted an earlier as-of read")
    expect_failure(
        container,
        rpc_sql(
            start_session_date=recurrent_base.session_date,
            end_session_date=recurrent_base.session_date,
            as_of=recurrent_a.observed_at,
        ),
        "pit_calendar_as_of_reader_timeline_ambiguous",
    )

    regression_base = calendar_fixture(day_offset=21, observed_minutes=10)
    append_calendar(container, regression_base)
    regression = reobserve(regression_base, minutes=-1)
    regression_receipt = append_calendar(container, regression)
    if regression_receipt.get("reason_code") != (
        "pit_calendar_observation_time_regressed"
    ):
        raise VerificationError("calendar clock regression was not quarantined")
    expect_failure(
        container,
        rpc_sql(
            start_session_date=regression_base.session_date,
            end_session_date=regression_base.session_date,
            as_of=regression.observed_at,
        ),
        "pit_calendar_as_of_reader_timeline_ambiguous",
    )

    same_clock_base = calendar_fixture(day_offset=22)
    same_clock_conflict = reobserve(
        same_clock_base,
        minutes=0,
        regular_end_delta_minutes=1,
    )
    append_calendar(container, same_clock_base)
    same_clock_receipt = append_calendar(container, same_clock_conflict)
    if same_clock_receipt.get("reason_code") != (
        "pit_calendar_revision_time_not_increasing"
    ):
        raise VerificationError("same-clock conflict was not quarantined")
    print("PASS all quarantine reasons, including no eligible accepted row")


def verify_cursor_pagination_and_fixed_snapshot(container: str) -> None:
    sessions = [calendar_fixture(day_offset=40 + index) for index in range(26)]
    for session in sessions:
        append_calendar(container, session)
    start = sessions[0].session_date
    end = sessions[-1].session_date + timedelta(days=1)
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    first = read_page(
        container,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=25,
    )
    cursor = first["next_cursor"]
    if not isinstance(cursor, dict):
        raise VerificationError("calendar pagination did not produce cursor")

    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor={"malformed": True},
        ),
        "pit_calendar_as_of_reader_cursor_invalid",
    )
    wrong_query = dict(cursor)
    wrong_query["query_sha256"] = "f" * 64
    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=wrong_query,
        ),
        "pit_calendar_as_of_reader_cursor_invalid",
    )
    expired = dict(cursor)
    expired["snapshot_issued_at"] = "2000-01-01T00:00:00Z"
    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=expired,
        ),
        "pit_calendar_as_of_reader_cursor_invalid",
    )
    spliced = dict(cursor)
    spliced["snapshot_manifest_sha256"] = "e" * 64
    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=spliced,
        ),
        "pit_calendar_as_of_reader_snapshot_mismatch",
    )
    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=26,
            cursor=cursor,
        ),
        "pit_calendar_as_of_reader_cursor_invalid",
    )

    appended = calendar_fixture(day_offset=66)
    with ThreadPoolExecutor(max_workers=2) as executor:
        append_future = executor.submit(append_calendar, container, appended)
        page_future = executor.submit(
            read_page,
            container,
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
            cursor=cursor,
        )
        append_receipt = append_future.result()
        second = page_future.result()
    if append_receipt.get("status") != "stored":
        raise VerificationError("concurrent calendar append did not store")
    snapshot_items = cast(list[dict[str, object]], first["items"]) + cast(
        list[dict[str, object]], second["items"]
    )
    if (
        first["candidate_count"] != 26
        or second["candidate_count"] != 26
        or second["next_cursor"] is not None
        or len(snapshot_items) != 26
        or first["snapshot_token"] != second["snapshot_token"]
        or first["snapshot_manifest_sha256"]
        != second["snapshot_manifest_sha256"]
        or manifest_for(snapshot_items) != first["snapshot_manifest_sha256"]
    ):
        raise VerificationError("fixed snapshot page drift/duplicate/gap detected")
    fresh = read_page(
        container,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
    )
    if fresh["candidate_count"] != 27 or len(fresh["items"]) != 27:
        raise VerificationError("fresh snapshot did not include concurrent append")

    quarantine_snapshot = read_page(
        container,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=25,
    )
    quarantine_cursor = quarantine_snapshot["next_cursor"]
    if not isinstance(quarantine_cursor, dict):
        raise VerificationError("quarantine snapshot did not produce cursor")
    same_clock_conflict = reobserve(
        sessions[0],
        minutes=0,
        regular_end_delta_minutes=1,
    )
    quarantine_receipt = append_calendar(container, same_clock_conflict)
    if quarantine_receipt.get("reason_code") != (
        "pit_calendar_revision_time_not_increasing"
    ):
        raise VerificationError("concurrent ambiguity was not quarantined")
    quarantine_second = read_page(
        container,
        start_session_date=start,
        end_session_date=end,
        as_of=as_of,
        limit=25,
        cursor=quarantine_cursor,
    )
    quarantine_snapshot_items = cast(
        list[dict[str, object]], quarantine_snapshot["items"]
    ) + cast(list[dict[str, object]], quarantine_second["items"])
    if (
        quarantine_second["candidate_count"] != 27
        or quarantine_second["snapshot_token"]
        != quarantine_snapshot["snapshot_token"]
        or manifest_for(quarantine_snapshot_items)
        != quarantine_snapshot["snapshot_manifest_sha256"]
    ):
        raise VerificationError("fixed snapshot was polluted by later quarantine")
    expect_failure(
        container,
        rpc_sql(
            start_session_date=start,
            end_session_date=end,
            as_of=as_of,
            limit=25,
        ),
        "pit_calendar_as_of_reader_timeline_ambiguous",
    )
    print(
        "PASS 2+ pages, tamper/expiry/splice, accepted append and quarantine "
        "fixed snapshots"
    )


def insert_reobservations(
    container: str,
    session: PointInTimeKrDailySessionV1,
    *,
    first_serial: int,
    last_serial: int,
) -> None:
    psql(
        container,
        f"""
insert into private.pit_calendar_observation_occurrences (
  calendar_idempotency_key,
  canonical_evidence_sha256,
  content_revision_id,
  observed_at,
  observation_payload,
  record_origin,
  received_at
)
select
  revision.calendar_idempotency_key,
  revision.canonical_evidence_sha256,
  revision.id,
  revision.observed_at + pg_catalog.make_interval(secs => generated.serial),
  pg_catalog.jsonb_set(
    revision.calendar_payload,
    '{{observed_at}}',
    pg_catalog.to_jsonb(private.pit_canonical_timestamp_v1(
      revision.observed_at + pg_catalog.make_interval(secs => generated.serial)
    )),
    false
  ),
  'rpc',
  pg_catalog.clock_timestamp()
from private.pit_calendar_content_revisions as revision
cross join pg_catalog.generate_series(
  {first_serial}, {last_serial}
) as generated(serial)
where revision.calendar_idempotency_key={sql_text(session.idempotency_key)}
  and revision.revision=1;
""",
    )


def verify_candidate_capacity(container: str) -> None:
    session = calendar_fixture(day_offset=80)
    append_calendar(container, session)
    insert_reobservations(container, session, first_serial=1, last_serial=999)
    items, pages = drain_pages(
        container,
        start_session_date=session.session_date,
        end_session_date=session.session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        limit=100,
    )
    if (
        len(items) != 1_000
        or len(pages) != 10
        or {page["candidate_count"] for page in pages} != {1_000}
        or manifest_for(items) != pages[0]["snapshot_manifest_sha256"]
    ):
        raise VerificationError("1000-candidate boundary did not pass exactly")
    insert_reobservations(container, session, first_serial=1000, last_serial=1000)
    expect_failure(
        container,
        rpc_sql(
            start_session_date=session.session_date,
            end_session_date=session.session_date,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        "pit_calendar_as_of_reader_candidate_limit_exceeded",
    )
    print("PASS 1000 candidate boundary and 1001 fail-closed limit")


def tamper_and_expect_integrity_failure(
    container: str,
    *,
    mutation: str,
    session: PointInTimeKrDailySessionV1,
    expected: str = "pit_calendar_as_of_reader_integrity_violation",
) -> None:
    expect_failure(
        container,
        f"""
begin;
set local session_replication_role = replica;
{mutation}
{rpc_sql(
    start_session_date=session.session_date,
    end_session_date=session.session_date,
    as_of=datetime(2026, 1, 1, tzinfo=UTC),
)}
""",
        expected,
    )


def commit_tamper_and_expect_integrity_failure(
    container: str,
    *,
    mutation: str,
    session: PointInTimeKrDailySessionV1,
) -> None:
    psql(
        container,
        f"""
begin;
set local session_replication_role = replica;
{mutation}
commit;
""",
    )
    expect_failure(
        container,
        rpc_sql(
            start_session_date=session.session_date,
            end_session_date=session.session_date,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        "pit_calendar_as_of_reader_integrity_violation",
    )


def verify_malformed_lineage_and_clocks(container: str) -> None:
    gap_session = calendar_fixture(day_offset=90)
    append_calendar(container, gap_session)
    tamper_and_expect_integrity_failure(
        container,
        session=gap_session,
        mutation=f"""
update private.pit_calendar_content_revisions
set revision=2
where calendar_idempotency_key={sql_text(gap_session.idempotency_key)};
""",
    )
    print("PASS malformed revision gap rejected")

    clock_session = calendar_fixture(day_offset=91)
    append_calendar(container, clock_session)
    tamper_and_expect_integrity_failure(
        container,
        session=clock_session,
        mutation=f"""
update private.pit_calendar_observation_occurrences
set received_at=observed_at - interval '1 second'
where calendar_idempotency_key={sql_text(clock_session.idempotency_key)};
""",
    )
    print("PASS malformed occurrence clock rejected")

    hash_session = calendar_fixture(day_offset=92)
    append_calendar(container, hash_session)
    bad_contract_hash = "e" * 64
    commit_tamper_and_expect_integrity_failure(
        container,
        session=hash_session,
        mutation=f"""
update private.pit_calendar_content_revisions
set calendar_payload=pg_catalog.jsonb_set(
      calendar_payload,'{{provider_contract_sha256}}',
      pg_catalog.to_jsonb({sql_text(bad_contract_hash)}::text),false)
where calendar_idempotency_key={sql_text(hash_session.idempotency_key)};
update private.pit_calendar_observation_occurrences
set observation_payload=pg_catalog.jsonb_set(
      observation_payload,'{{provider_contract_sha256}}',
      pg_catalog.to_jsonb({sql_text(bad_contract_hash)}::text),false)
where calendar_idempotency_key={sql_text(hash_session.idempotency_key)};
""",
    )
    print("PASS malformed evidence hash rejected")

    orphan_session = calendar_fixture(day_offset=93)
    append_calendar(container, orphan_session)
    commit_tamper_and_expect_integrity_failure(
        container,
        session=orphan_session,
        mutation=f"""
update private.pit_calendar_observation_occurrences
set content_revision_id=pg_catalog.gen_random_uuid()
where calendar_idempotency_key={sql_text(orphan_session.idempotency_key)};
""",
    )
    print("PASS malformed composite lineage rejected")

    null_content_clock_session = calendar_fixture(day_offset=94)
    append_calendar(container, null_content_clock_session)
    commit_tamper_and_expect_integrity_failure(
        container,
        session=null_content_clock_session,
        mutation=f"""
update private.pit_calendar_content_revisions
set calendar_payload=pg_catalog.jsonb_set(
      calendar_payload,'{{observed_at}}','null'::jsonb,false)
where calendar_idempotency_key=
  {sql_text(null_content_clock_session.idempotency_key)};
""",
    )
    print("PASS JSON-null content observation clock rejected")
    print(
        "PASS malformed revision, hash, clock, JSON null and composite "
        "lineage rejection"
    )


def verify_acl_rls_owner_and_zero_write(container: str) -> None:
    owner_acl = scalar(
        container,
        f"""
with functions as (
  select p.oid, p.prosecdef, p.provolatile, p.proconfig,
         pg_catalog.pg_get_userbyid(p.proowner) as owner_name
  from pg_catalog.pg_proc as p
  where p.oid in (
    {sql_text(CALENDAR_IDENTITY_FUNCTION)}::regprocedure,
    {sql_text(CALENDAR_EVIDENCE_FUNCTION)}::regprocedure,
    {sql_text(CANDIDATE_FUNCTION)}::regprocedure,
    {sql_text(PRIVATE_READER_FUNCTION)}::regprocedure,
    {sql_text(WORKER_READER_FUNCTION)}::regprocedure
  )
)
select concat_ws('|',
  (select count(*)=5 from functions),
  (select count(distinct owner_name)=1 from functions),
  (select bool_and(owner_name not in
      ('anon','authenticated','authenticator','service_role')) from functions),
  (select prosecdef and provolatile='s' and
          proconfig=array['search_path=""']::text[]
   from functions where oid={sql_text(CANDIDATE_FUNCTION)}::regprocedure),
  (select prosecdef and provolatile='s' and
          proconfig=array['search_path=""']::text[]
   from functions where oid={sql_text(PRIVATE_READER_FUNCTION)}::regprocedure),
  (select not prosecdef and provolatile='s' and
          proconfig=array['search_path=""']::text[]
   from functions where oid={sql_text(WORKER_READER_FUNCTION)}::regprocedure),
  (select count(*)=2 and bool_and(
          prosecdef and provolatile='i' and
          proconfig=array['search_path=""']::text[])
   from functions where oid in (
     {sql_text(CALENDAR_IDENTITY_FUNCTION)}::regprocedure,
     {sql_text(CALENDAR_EVIDENCE_FUNCTION)}::regprocedure
   )),
  not pg_catalog.has_function_privilege(
    'public',{sql_text(WORKER_READER_FUNCTION)},'EXECUTE'),
  pg_catalog.has_function_privilege(
    'service_role',{sql_text(WORKER_READER_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'public',{sql_text(PRIVATE_READER_FUNCTION)},'EXECUTE'),
  pg_catalog.has_function_privilege(
    'service_role',{sql_text(PRIVATE_READER_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'service_role',{sql_text(CANDIDATE_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'public',{sql_text(CALENDAR_IDENTITY_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'service_role',{sql_text(CALENDAR_IDENTITY_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'public',{sql_text(CALENDAR_EVIDENCE_FUNCTION)},'EXECUTE'),
  not pg_catalog.has_function_privilege(
    'service_role',{sql_text(CALENDAR_EVIDENCE_FUNCTION)},'EXECUTE')
);
""",
    )
    if owner_acl != "|".join(["t"] * 16):
        raise VerificationError(f"calendar reader owner/ACL mismatch: {owner_acl}")

    rls = scalar(
        container,
        """
select concat_ws('|',
  (select bool_and(c.relrowsecurity)
   from pg_catalog.pg_class as c
   join pg_catalog.pg_namespace as n on n.oid=c.relnamespace
   where n.nspname='private'
     and c.relname in (
       'pit_calendar_stream_heads',
       'pit_calendar_content_revisions',
       'pit_calendar_observation_occurrences',
       'pit_calendar_observation_quarantine'
     )),
  (select count(*)=0
   from pg_catalog.pg_policy as p
   join pg_catalog.pg_class as c on c.oid=p.polrelid
   join pg_catalog.pg_namespace as n on n.oid=c.relnamespace
   where n.nspname='private'
     and c.relname in (
       'pit_calendar_stream_heads',
       'pit_calendar_content_revisions',
       'pit_calendar_observation_occurrences',
       'pit_calendar_observation_quarantine'
     ))
);
""",
    )
    if rls != "t|t":
        raise VerificationError(f"calendar private RLS mismatch: {rls}")

    base_call = """
select worker_api.list_pit_kr_daily_sessions_as_of_v1(
  'toss','KR','2025-01-01'::date,'2025-01-02'::date,
  '2026-01-01T00:00:00Z'::timestamptz,100,null
);
"""
    for role in ("anon", "authenticated", "authenticator"):
        expect_failure(
            container,
            f"set role {role}; {base_call}",
            "permission denied",
        )
    for role in ("anon", "authenticated", "authenticator", "service_role"):
        for table in PRIVATE_TABLES:
            expect_failure(
                container,
                f"set role {role}; select count(*) from private.{table};",
                "permission denied",
            )
    expect_failure(
        container,
        """
set role service_role;
select count(*) from private.pit_kr_calendar_as_of_candidates_v1(
  'toss','KR','2025-01-01'::date,'2025-01-02'::date,
  '2026-01-01T00:00:00Z'::timestamptz,pg_current_snapshot()
);
""",
        "permission denied",
    )

    zero_write_sessions = [
        calendar_fixture(day_offset=250 + index) for index in range(26)
    ]
    for session in zero_write_sessions:
        append_calendar(container, session)
    before = table_fingerprint(container)
    first = read_page(
        container,
        start_session_date=zero_write_sessions[0].session_date,
        end_session_date=zero_write_sessions[-1].session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        limit=25,
    )
    cursor = first["next_cursor"]
    if not isinstance(cursor, dict):
        raise VerificationError("zero-write continuation cursor missing")
    second = read_page(
        container,
        start_session_date=zero_write_sessions[0].session_date,
        end_session_date=zero_write_sessions[-1].session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        limit=25,
        cursor=cursor,
    )
    if second["next_cursor"] is not None:
        raise VerificationError("zero-write continuation did not terminate")
    tampered_cursor = dict(cursor)
    tampered_cursor["query_sha256"] = "f" * 64
    expect_failure(
        container,
        rpc_sql(
            start_session_date=zero_write_sessions[0].session_date,
            end_session_date=zero_write_sessions[-1].session_date,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            limit=25,
            cursor=tampered_cursor,
        ),
        "pit_calendar_as_of_reader_cursor_invalid",
    )
    expect_failure(
        container,
        rpc_sql(
            start_session_date=BASE_SESSION_DATE,
            end_session_date=BASE_SESSION_DATE + timedelta(days=1),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            limit=24,
        ),
        "pit_calendar_as_of_reader_argument_invalid",
    )
    after = table_fingerprint(container)
    if before != after:
        raise VerificationError("calendar reader changed domain/trading/order rows")
    print("PASS ACL/RLS/owner/search_path/helper denial and zero writes")


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("calendar as-of reader migration is missing") from exc
    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    existing = calendar_fixture(day_offset=200)
    append_calendar(container, existing)
    calendar_before = scalar(
        container,
        """
select md5(string_agg(to_jsonb(rows)::text, E'\n'
order by to_jsonb(rows)::text))
from (
  select 'head' as source, to_jsonb(value) as row_value
  from private.pit_calendar_stream_heads as value
  union all
  select 'revision', to_jsonb(value)
  from private.pit_calendar_content_revisions as value
  union all
  select 'occurrence', to_jsonb(value)
  from private.pit_calendar_observation_occurrences as value
  union all
  select 'quarantine', to_jsonb(value)
  from private.pit_calendar_observation_quarantine as value
) as rows;
""",
    )
    settings_before = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings where settings.id='singleton';
""",
    )
    psql(container, target.read_text(encoding="utf-8"))
    initial = read_page(
        container,
        start_session_date=existing.session_date,
        end_session_date=existing.session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if initial["candidate_count"] != 1 or len(initial["items"]) != 1:
        raise VerificationError("target migration could not read populated row")
    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))

    calendar_after = scalar(
        container,
        """
select md5(string_agg(to_jsonb(rows)::text, E'\n'
order by to_jsonb(rows)::text))
from (
  select 'head' as source, to_jsonb(value) as row_value
  from private.pit_calendar_stream_heads as value
  union all
  select 'revision', to_jsonb(value)
  from private.pit_calendar_content_revisions as value
  union all
  select 'occurrence', to_jsonb(value)
  from private.pit_calendar_observation_occurrences as value
  union all
  select 'quarantine', to_jsonb(value)
  from private.pit_calendar_observation_quarantine as value
) as rows;
""",
    )
    settings_after = scalar(
        container,
        """
select md5(row_to_json(settings)::text)
from public.bot_settings as settings where settings.id='singleton';
""",
    )
    converged = read_page(
        container,
        start_session_date=existing.session_date,
        end_session_date=existing.session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if (
        calendar_after != calendar_before
        or settings_after != settings_before
        or converged["items"] != initial["items"]
        or converged["snapshot_manifest_sha256"]
        != initial["snapshot_manifest_sha256"]
    ):
        raise VerificationError("populated upgrade/future convergence changed data")

    added = calendar_fixture(day_offset=201)
    append_calendar(container, added)
    after_write = read_page(
        container,
        start_session_date=existing.session_date,
        end_session_date=added.session_date,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if after_write["candidate_count"] != 2 or len(after_write["items"]) != 2:
        raise VerificationError("post-upgrade append/read did not converge")
    print("PASS populated target upgrade and post-target migration convergence")


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-calendar-reader-fresh-{suffix}"
    upgrade = f"msp-pit-calendar-reader-upgrade-{suffix}"
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
        verify_empty_open_closed_and_hash_vectors(fresh)
        verify_replay_correction_and_cutoff(fresh)
        verify_quarantine_blocks_complete_timeline(fresh)
        verify_cursor_pagination_and_fixed_snapshot(fresh)
        verify_candidate_capacity(fresh)
        verify_malformed_lineage_and_clocks(fresh)
        verify_acl_rls_owner_and_zero_write(fresh)
        verify_populated_upgrade(upgrade)
        print("FINAL=PASS pit_calendar_as_of_reader_verifier")
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
