from __future__ import annotations

import sqlite3
from datetime import timedelta

from msp.db import row_to_dict
from msp.domain.enums import OrderSide, OrderStatus, SystemMode
from msp.domain.models import BrokerOrderCommand, BrokerOrderResult
from msp.exceptions import IdempotencyError, NotFoundError, SafetyError
from msp.services.audit import append_audit, append_outbox
from msp.services.ids import broker_client_order_id, new_id, normalize_symbol
from msp.services.system_state import ORDER_GENERATION_MODES, TRADING_MODES, get_system_state
from msp.settings import Settings
from msp.time import utc_now, utc_now_iso


def _require_idempotency(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise IdempotencyError("Idempotency-Key is required for order changes")
    return idempotency_key


def create_order_intent(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    idempotency_key: str | None,
    actor: str,
    symbol: str,
    side: OrderSide,
    quantity: float,
    limit_price: float,
    currency: str,
    approved: bool,
    priority: int = 0,
    approval_minutes: int = 60,
    portfolio_hash: str | None = None,
    max_notional: float | None = None,
    max_slippage_bps: int | None = None,
) -> dict:
    idempotency_key = _require_idempotency(idempotency_key)
    existing = conn.execute(
        "SELECT * FROM order_intents WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return row_to_dict(existing) or {}

    state = get_system_state(conn)
    if state["mode"] not in ORDER_GENERATION_MODES:
        raise SafetyError(f"order generation is disabled in {state['mode']} mode")
    if state["mode"] == SystemMode.LIVE.value and not settings.allow_live_mode:
        raise SafetyError("LIVE mode is disabled")

    if quantity <= 0:
        raise SafetyError("quantity must be positive")
    if limit_price <= 0:
        raise SafetyError("limit_price must be positive")

    normalized_symbol = normalize_symbol(symbol)
    now = utc_now()
    now_iso = now.isoformat()
    approved_at = now_iso if approved else None
    approval_expires_at = (now + timedelta(minutes=approval_minutes)).isoformat() if approved else None
    status = OrderStatus.READY.value if approved else OrderStatus.DRAFT.value
    intent_id = new_id("intent")
    internal_order_id = new_id("internal")
    client_order_id = broker_client_order_id(idempotency_key)
    notional = quantity * limit_price
    if max_notional is not None and notional > max_notional:
        raise SafetyError("order notional exceeds approval max_notional")

    conn.execute(
        """
        INSERT INTO order_intents
            (
                id, account_id, symbol, side, quantity, limit_price, currency, status,
                approved_at, approval_expires_at, priority, idempotency_key,
                internal_order_id, broker_client_order_id, attempt, execution_owner,
                fencing_token, reject_reason, portfolio_hash, max_notional,
                max_slippage_bps, created_at, updated_at
            )
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            settings.account_id,
            normalized_symbol,
            side.value,
            quantity,
            limit_price,
            currency.upper(),
            status,
            approved_at,
            approval_expires_at,
            priority,
            idempotency_key,
            internal_order_id,
            client_order_id,
            portfolio_hash,
            max_notional,
            max_slippage_bps,
            now_iso,
            now_iso,
        ),
    )
    payload = {
        "order_intent_id": intent_id,
        "symbol": normalized_symbol,
        "side": side.value,
        "quantity": quantity,
        "limit_price": limit_price,
        "status": status,
    }
    append_audit(conn, "order_intent.created", actor, payload, idempotency_key)
    append_outbox(conn, "order_intent", intent_id, "order_intent.created", payload)
    return get_order_intent(conn, intent_id)


def approve_order_intent(
    conn: sqlite3.Connection,
    *,
    intent_id: str,
    idempotency_key: str | None,
    actor: str,
    max_notional: float,
    max_slippage_bps: int,
    expires_minutes: int,
    portfolio_hash: str | None,
    reason: str,
) -> dict:
    idempotency_key = _require_idempotency(idempotency_key)
    intent = get_order_intent(conn, intent_id)
    if intent["status"] not in {OrderStatus.DRAFT.value, OrderStatus.RISK_APPROVED.value, OrderStatus.READY.value}:
        raise SafetyError(f"cannot approve order in {intent['status']} status")

    notional = float(intent["quantity"]) * float(intent["limit_price"])
    if notional > max_notional:
        raise SafetyError("order notional exceeds approval max_notional")

    now = utc_now()
    expires_at = (now + timedelta(minutes=expires_minutes)).isoformat()
    conn.execute(
        """
        UPDATE order_intents
        SET status = 'READY',
            approved_at = ?,
            approval_expires_at = ?,
            max_notional = ?,
            max_slippage_bps = ?,
            portfolio_hash = COALESCE(?, portfolio_hash),
            updated_at = ?
        WHERE id = ?
        """,
        (
            now.isoformat(),
            expires_at,
            max_notional,
            max_slippage_bps,
            portfolio_hash,
            now.isoformat(),
            intent_id,
        ),
    )
    payload = {
        "order_intent_id": intent_id,
        "max_notional": max_notional,
        "max_slippage_bps": max_slippage_bps,
        "expires_at": expires_at,
        "reason": reason,
    }
    append_audit(conn, "order_intent.approved", actor, payload, idempotency_key)
    append_outbox(conn, "order_intent", intent_id, "order_intent.approved", payload)
    return get_order_intent(conn, intent_id)


def get_order_intent(conn: sqlite3.Connection, intent_id: str) -> dict:
    row = conn.execute("SELECT * FROM order_intents WHERE id = ?", (intent_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"order intent not found: {intent_id}")
    return row_to_dict(row) or {}


def list_order_intents(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM order_intents
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def claim_next_ready_order(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    owner: str,
    fencing_token: int,
) -> dict | None:
    state = get_system_state(conn)
    if state["mode"] not in TRADING_MODES:
        return None

    now = utc_now_iso()
    row = conn.execute(
        """
        SELECT *
        FROM order_intents
        WHERE account_id = ?
          AND status = 'READY'
          AND approved_at IS NOT NULL
          AND approval_expires_at > ?
        ORDER BY priority DESC, created_at ASC
        LIMIT 1
        """,
        (account_id, now),
    ).fetchone()
    if row is None:
        return None

    conn.execute(
        """
        UPDATE order_intents
        SET status = 'SUBMITTING',
            execution_owner = ?,
            fencing_token = ?,
            attempt = attempt + 1,
            updated_at = ?
        WHERE id = ? AND status = 'READY'
        """,
        (owner, fencing_token, now, row["id"]),
    )
    return get_order_intent(conn, row["id"])


def reset_submitting_to_ready(conn: sqlite3.Connection, *, intent_id: str, reason: str) -> dict:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE order_intents
        SET status = 'READY', reject_reason = ?, updated_at = ?
        WHERE id = ? AND status = 'SUBMITTING'
        """,
        (reason, now, intent_id),
    )
    return get_order_intent(conn, intent_id)


def command_from_intent(intent: dict) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        account_id=intent["account_id"],
        order_intent_id=intent["id"],
        broker_client_order_id=intent["broker_client_order_id"],
        symbol=intent["symbol"],
        side=OrderSide(intent["side"]),
        quantity=float(intent["quantity"]),
        limit_price=float(intent["limit_price"]),
        currency=intent["currency"],
    )


def complete_intent_from_broker_result(
    conn: sqlite3.Connection,
    *,
    intent: dict,
    result: BrokerOrderResult,
    actor: str,
) -> dict:
    now = utc_now_iso()
    next_status = result.status
    reject_reason = None
    if result.status == "REJECTED":
        reject_reason = result.raw_response.get("reason", "broker rejected order")
    elif result.status not in {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
    }:
        next_status = "UNKNOWN"
        reject_reason = "broker response was ambiguous"

    conn.execute(
        """
        UPDATE order_intents
        SET status = ?, reject_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (next_status, reject_reason, now, intent["id"]),
    )
    payload = {
        "order_intent_id": intent["id"],
        "broker_order_id": result.broker_order_id,
        "status": next_status,
        "filled_quantity": result.filled_quantity,
        "average_price": result.average_price,
    }
    append_audit(conn, "order_intent.broker_result", actor, payload)
    append_outbox(conn, "order_intent", intent["id"], "order_intent.broker_result", payload)
    return get_order_intent(conn, intent["id"])
