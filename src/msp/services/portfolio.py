from __future__ import annotations

import hashlib
import json
import sqlite3

from msp.adapters.paper import PaperBrokerAdapter
from msp.db import row_to_dict
from msp.domain.enums import OrderSide
from msp.exceptions import IdempotencyError, NotFoundError, SafetyError
from msp.services.audit import append_audit, append_outbox
from msp.services.ids import new_id
from msp.services.orders import create_order_intent
from msp.services.research import FEATURE_VERSION
from msp.time import utc_now_iso


def _hash_targets(targets: list[dict]) -> str:
    encoded = json.dumps(targets, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def generate_rebalance(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    as_of_date: str | None = None,
    top_n: int = 2,
    gross_exposure: float = 0.5,
    actor: str = "portfolio-worker",
) -> dict:
    if top_n <= 0:
        raise SafetyError("top_n must be positive")
    if gross_exposure <= 0 or gross_exposure > 1:
        raise SafetyError("gross_exposure must be between 0 and 1")

    if as_of_date is None:
        row = conn.execute("SELECT MAX(as_of_date) AS as_of_date FROM research_scores").fetchone()
        as_of_date = row["as_of_date"] if row else None
    if not as_of_date:
        raise SafetyError("no research scores available; run research worker first")

    score_rows = conn.execute(
        """
        SELECT symbol, total_score
        FROM research_scores
        WHERE as_of_date = ? AND feature_version = ?
        ORDER BY total_score DESC
        LIMIT ?
        """,
        (as_of_date, FEATURE_VERSION, top_n),
    ).fetchall()
    if not score_rows:
        raise SafetyError("no scores for requested date")

    broker = PaperBrokerAdapter(conn)
    cash_rows = broker.list_cash(account_id)
    krw_cash = next((float(row["balance"]) for row in cash_rows if row["currency"] == "KRW"), 0.0)
    total_notional = krw_cash * gross_exposure
    if total_notional <= 0:
        raise SafetyError("no KRW cash available for demo rebalance")

    target_notional = total_notional / len(score_rows)
    targets = []
    for row in score_rows:
        price_row = conn.execute(
            """
            SELECT close
            FROM price_bars_daily
            WHERE symbol = ? AND as_of_date <= ?
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (row["symbol"], as_of_date),
        ).fetchone()
        if price_row is None:
            continue
        targets.append(
            {
                "symbol": row["symbol"],
                "target_weight": round(1.0 / len(score_rows), 6),
                "target_notional": round(target_notional, 2),
                "reference_price": float(price_row["close"]),
                "score": float(row["total_score"]),
            }
        )
    if not targets:
        raise SafetyError("no targetable instruments")

    portfolio_hash = _hash_targets(targets)
    rebalance_id = new_id("rebalance")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO rebalance_runs
            (id, account_id, as_of_date, status, portfolio_hash, total_notional, created_at)
        VALUES (?, ?, ?, 'PROPOSED', ?, ?, ?)
        """,
        (rebalance_id, account_id, as_of_date, portfolio_hash, total_notional, now),
    )
    for target in targets:
        conn.execute(
            """
            INSERT INTO rebalance_targets
                (id, rebalance_id, symbol, target_weight, target_notional, reference_price, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("target"),
                rebalance_id,
                target["symbol"],
                target["target_weight"],
                target["target_notional"],
                target["reference_price"],
                target["score"],
                now,
            ),
        )

    payload = {
        "rebalance_id": rebalance_id,
        "as_of_date": as_of_date,
        "portfolio_hash": portfolio_hash,
        "target_count": len(targets),
        "total_notional": total_notional,
    }
    append_audit(conn, "portfolio.rebalance_proposed", actor, payload)
    append_outbox(conn, "rebalance", rebalance_id, "portfolio.rebalance_proposed", payload)
    return get_rebalance(conn, rebalance_id)


def approve_rebalance(
    conn: sqlite3.Connection,
    *,
    settings,
    rebalance_id: str,
    portfolio_hash: str,
    max_notional: float,
    max_slippage_bps: int,
    idempotency_key: str | None,
    actor: str = "api",
    create_orders: bool = True,
) -> dict:
    if not idempotency_key:
        raise IdempotencyError("Idempotency-Key is required for rebalance approval")
    rebalance = get_rebalance(conn, rebalance_id)
    if rebalance["status"] == "APPROVED":
        return rebalance
    if rebalance["status"] != "PROPOSED":
        raise SafetyError(f"cannot approve rebalance in {rebalance['status']} status")
    if rebalance["portfolio_hash"] != portfolio_hash:
        raise SafetyError("portfolio_hash mismatch; proposal changed or approval payload is stale")
    if float(rebalance["total_notional"]) > max_notional:
        raise SafetyError("rebalance total_notional exceeds max_notional")

    now = utc_now_iso()
    conn.execute(
        """
        UPDATE rebalance_runs
        SET status = 'APPROVED',
            approved_at = ?,
            approval_idempotency_key = ?
        WHERE id = ?
        """,
        (now, idempotency_key, rebalance_id),
    )
    created_orders = []
    if create_orders:
        for target in rebalance["targets"]:
            quantity = int(float(target["target_notional"]) / float(target["reference_price"]))
            if quantity <= 0:
                continue
            intent = create_order_intent(
                conn,
                settings=settings,
                idempotency_key=f"{idempotency_key}-{target['symbol']}",
                actor=actor,
                symbol=target["symbol"],
                side=OrderSide.BUY,
                quantity=quantity,
                limit_price=float(target["reference_price"]),
                currency="KRW",
                approved=True,
                max_notional=float(target["target_notional"]),
                max_slippage_bps=max_slippage_bps,
                portfolio_hash=portfolio_hash,
            )
            created_orders.append(intent["id"])

    payload = {
        "rebalance_id": rebalance_id,
        "portfolio_hash": portfolio_hash,
        "created_order_intents": created_orders,
    }
    append_audit(conn, "portfolio.rebalance_approved", actor, payload, idempotency_key)
    append_outbox(conn, "rebalance", rebalance_id, "portfolio.rebalance_approved", payload)
    result = get_rebalance(conn, rebalance_id)
    result["created_order_intents"] = created_orders
    return result


def get_rebalance(conn: sqlite3.Connection, rebalance_id: str) -> dict:
    row = conn.execute("SELECT * FROM rebalance_runs WHERE id = ?", (rebalance_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"rebalance not found: {rebalance_id}")
    result = row_to_dict(row) or {}
    target_rows = conn.execute(
        """
        SELECT symbol, target_weight, target_notional, reference_price, score
        FROM rebalance_targets
        WHERE rebalance_id = ?
        ORDER BY score DESC
        """,
        (rebalance_id,),
    ).fetchall()
    result["targets"] = [{key: target[key] for key in target.keys()} for target in target_rows]
    return result


def list_rebalances(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id
        FROM rebalance_runs
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [get_rebalance(conn, row["id"]) for row in rows]
