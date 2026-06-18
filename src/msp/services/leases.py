from __future__ import annotations

import sqlite3
from datetime import timedelta

from msp.time import utc_now, utc_now_iso


def acquire_execution_lease(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    owner: str,
    ttl_seconds: int,
) -> dict | None:
    now = utc_now()
    now_iso = now.isoformat()
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    row = conn.execute(
        "SELECT * FROM execution_leases WHERE account_id = ?",
        (account_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO execution_leases (account_id, owner, fencing_token, expires_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (account_id, owner, expires_at, now_iso),
        )
        return {"account_id": account_id, "owner": owner, "fencing_token": 1, "expires_at": expires_at}

    if row["owner"] == owner or row["expires_at"] < now_iso:
        next_token = int(row["fencing_token"]) + (0 if row["owner"] == owner else 1)
        conn.execute(
            """
            UPDATE execution_leases
            SET owner = ?, fencing_token = ?, expires_at = ?, updated_at = ?
            WHERE account_id = ?
            """,
            (owner, next_token, expires_at, now_iso, account_id),
        )
        return {
            "account_id": account_id,
            "owner": owner,
            "fencing_token": next_token,
            "expires_at": expires_at,
        }

    return None


def get_execution_lease(conn: sqlite3.Connection, account_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM execution_leases WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
