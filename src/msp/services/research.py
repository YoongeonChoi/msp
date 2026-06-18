from __future__ import annotations

import sqlite3

from msp.exceptions import SafetyError
from msp.services.audit import append_audit, append_outbox
from msp.services.data_platform import latest_market_date
from msp.time import utc_now_iso


FEATURE_VERSION = "demo-factors-v1"


def compute_demo_scores(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    actor: str = "research-worker",
) -> dict:
    as_of_date = as_of_date or latest_market_date(conn)
    if not as_of_date:
        raise SafetyError("no market data available; run data worker first")

    instruments = conn.execute(
        """
        SELECT symbol
        FROM instruments
        WHERE active = 1
        ORDER BY symbol
        """
    ).fetchall()
    if not instruments:
        raise SafetyError("no instruments available; run data worker first")

    created = 0
    now = utc_now_iso()
    for instrument in instruments:
        rows = conn.execute(
            """
            SELECT close, volume
            FROM price_bars_daily
            WHERE symbol = ? AND as_of_date <= ?
            ORDER BY as_of_date ASC
            """,
            (instrument["symbol"], as_of_date),
        ).fetchall()
        if len(rows) < 2:
            continue
        first_close = float(rows[0]["close"])
        last_close = float(rows[-1]["close"])
        momentum = max(-1.0, min(1.0, ((last_close / first_close) - 1.0) * 10.0))
        avg_volume = sum(float(row["volume"]) for row in rows) / len(rows)
        quality = max(0.0, min(1.0, avg_volume / 2_000_000.0))
        ai_event = 0.0
        total = (0.7 * momentum) + (0.2 * quality) + (0.1 * ai_event)
        conn.execute(
            """
            INSERT INTO research_scores
                (
                    symbol, as_of_date, feature_version, momentum_score, quality_score,
                    ai_event_score, total_score, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, as_of_date, feature_version)
            DO UPDATE SET
                momentum_score = excluded.momentum_score,
                quality_score = excluded.quality_score,
                ai_event_score = excluded.ai_event_score,
                total_score = excluded.total_score,
                created_at = excluded.created_at
            """,
            (instrument["symbol"], as_of_date, FEATURE_VERSION, momentum, quality, ai_event, total, now),
        )
        created += 1

    payload = {"as_of_date": as_of_date, "feature_version": FEATURE_VERSION, "score_count": created}
    append_audit(conn, "research.scores_computed", actor, payload)
    append_outbox(conn, "research_scores", as_of_date, "research.scores_computed", payload)
    return payload


def list_scores(conn: sqlite3.Connection, *, as_of_date: str | None = None, limit: int = 100) -> list[dict]:
    params: tuple
    where = ""
    if as_of_date:
        where = "WHERE as_of_date = ?"
        params = (as_of_date, limit)
    else:
        params = (limit,)
    rows = conn.execute(
        f"""
        SELECT *
        FROM research_scores
        {where}
        ORDER BY as_of_date DESC, total_score DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]
