from __future__ import annotations

import json
import sqlite3

from msp.services.ids import new_id
from msp.time import utc_now_iso


def append_audit(
    conn: sqlite3.Connection,
    event_type: str,
    actor: str,
    payload: dict,
    idempotency_key: str | None = None,
) -> dict:
    row = {
        "id": new_id("audit"),
        "event_type": event_type,
        "actor": actor,
        "payload_json": json.dumps(payload, sort_keys=True),
        "idempotency_key": idempotency_key,
        "created_at": utc_now_iso(),
    }
    conn.execute(
        """
        INSERT INTO audit_events
            (id, event_type, actor, payload_json, idempotency_key, created_at)
        VALUES
            (:id, :event_type, :actor, :payload_json, :idempotency_key, :created_at)
        """,
        row,
    )
    return row


def append_outbox(
    conn: sqlite3.Connection,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict,
) -> dict:
    row = {
        "id": new_id("outbox"),
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
        "payload_json": json.dumps(payload, sort_keys=True),
        "published_at": None,
        "created_at": utc_now_iso(),
    }
    conn.execute(
        """
        INSERT INTO outbox_events
            (id, aggregate_type, aggregate_id, event_type, payload_json, published_at, created_at)
        VALUES
            (:id, :aggregate_type, :aggregate_id, :event_type, :payload_json, :published_at, :created_at)
        """,
        row,
    )
    return row
