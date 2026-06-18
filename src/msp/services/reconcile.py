from __future__ import annotations

import json
import sqlite3

from msp.adapters.paper import PaperBrokerAdapter
from msp.adapters.base import BrokerAdapter
from msp.services.audit import append_audit
from msp.services.ids import new_id
from msp.services.system_state import force_halt
from msp.time import utc_now_iso


def reconcile_account(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    actor: str = "reconcile-worker",
    broker: BrokerAdapter | None = None,
) -> dict:
    broker = broker or PaperBrokerAdapter(conn)
    now = utc_now_iso()
    run_id = new_id("reconcile")
    mismatch_count = 0
    details: dict = {"unknown_orders": [], "snapshotted_positions": 0, "snapshotted_cash": 0}

    positions = broker.list_positions(account_id)
    for position in positions:
        conn.execute(
            """
            INSERT INTO position_snapshots
                (id, account_id, symbol, quantity, avg_cost, market_price, currency, as_of)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("pos_snap"),
                account_id,
                position["symbol"],
                position["quantity"],
                position["avg_cost"],
                position["market_price"],
                position["currency"],
                now,
            ),
        )
    details["snapshotted_positions"] = len(positions)

    cash_rows = broker.list_cash(account_id)
    for cash in cash_rows:
        conn.execute(
            """
            INSERT INTO cash_snapshots
                (id, account_id, currency, balance, as_of)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("cash_snap"), account_id, cash["currency"], cash["balance"], now),
        )
    details["snapshotted_cash"] = len(cash_rows)

    unknown_orders = conn.execute(
        """
        SELECT id, symbol, side, quantity, broker_client_order_id
        FROM order_intents
        WHERE account_id = ? AND status = 'UNKNOWN'
        """,
        (account_id,),
    ).fetchall()
    if unknown_orders:
        mismatch_count += len(unknown_orders)
        details["unknown_orders"] = [
            {key: row[key] for key in row.keys()}
            for row in unknown_orders
        ]

    status = "MISMATCH" if mismatch_count else "OK"
    conn.execute(
        """
        INSERT INTO reconciliation_runs
            (id, account_id, status, mismatch_count, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, account_id, status, mismatch_count, json.dumps(details, sort_keys=True), now),
    )
    append_audit(
        conn,
        "reconciliation.completed",
        actor,
        {"run_id": run_id, "status": status, "mismatch_count": mismatch_count, **details},
    )
    if mismatch_count:
        force_halt(conn, actor=actor, reason="reconciliation mismatch or unknown order")
    return {"run_id": run_id, "status": status, "mismatch_count": mismatch_count, "details": details}
