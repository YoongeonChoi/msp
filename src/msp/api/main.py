from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from msp.adapters.paper import PaperBrokerAdapter
from msp.api.models import (
    ApproveOrderIntentRequest,
    ApproveRebalanceRequest,
    ArmRequest,
    CommandRequest,
    CreateOrderIntentRequest,
    GenerateRebalanceRequest,
    KillRequest,
    SeedCashRequest,
    UnlockRequest,
)
from msp.db import connect, init_db, row_to_dict, transaction
from msp.exceptions import MspError
from msp.services.audit import append_audit
from msp.services.data_platform import latest_market_date, seed_demo_market_data
from msp.services.leases import get_execution_lease
from msp.services.orders import approve_order_intent, create_order_intent, list_order_intents
from msp.services.portfolio import approve_rebalance, generate_rebalance, list_rebalances
from msp.services.research import FEATURE_VERSION, compute_demo_scores, list_scores
from msp.services.system_state import command_arm, command_halt, command_kill, command_unlock, get_system_state
from msp.settings import load_settings
from msp.workers.execution import run_once as run_execution_once
from msp.workers.reconcile import run_once as run_reconcile_once

settings = load_settings()
app = FastAPI(title="MSP Control API", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db(settings)


def _idempotency(header_value: str | None, body_value: str | None) -> str | None:
    return header_value or body_value


def _handle_error(exc: MspError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health/live")
def live() -> dict:
    return {"ok": True}


@app.get("/health/ready")
def ready() -> dict:
    with connect(settings) as conn:
        state = get_system_state(conn)
    return {"ok": True, "mode": state["mode"], "state_version": state["state_version"]}


@app.get("/v1/status")
def status() -> dict:
    with connect(settings) as conn:
        state = get_system_state(conn)
        broker = PaperBrokerAdapter(conn)
        lease = get_execution_lease(conn, settings.account_id)
        open_orders = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM order_intents
            WHERE status IN ('READY', 'SUBMITTING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'UNKNOWN')
            """
        ).fetchone()["count"]
        unknown_orders = conn.execute(
            "SELECT COUNT(*) AS count FROM order_intents WHERE status = 'UNKNOWN'"
        ).fetchone()["count"]
        last_reconcile = conn.execute(
            """
            SELECT * FROM reconciliation_runs
            WHERE account_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (settings.account_id,),
        ).fetchone()
        failed_jobs_24h = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reconciliation_runs
            WHERE status != 'OK'
            """
        ).fetchone()["count"]

        return {
            "system": {
                "mode": state["mode"],
                "state_version": state["state_version"],
                "trading_enabled": bool(state["trading_enabled"]),
                "environment": settings.environment,
                "allow_live_mode": settings.allow_live_mode,
                "account_id": settings.account_id,
            },
            "broker": {
                "configured_adapter": settings.broker_adapter,
                "active_adapter": "paper" if state["mode"] != "LIVE" else settings.broker_adapter,
                "real_broker_enabled": settings.enable_real_broker,
                "status": "UP",
                "cash": broker.list_cash(settings.account_id),
                "positions": broker.list_positions(settings.account_id),
            },
            "execution": {
                "lease": lease,
                "open_orders": open_orders,
                "unknown_orders": unknown_orders,
                "reconciled": unknown_orders == 0,
            },
            "data": {
                "kr_prices_as_of": latest_market_date(conn),
                "us_prices_as_of": latest_market_date(conn),
                "source": "demo" if latest_market_date(conn) else "not configured",
            },
            "models": {
                "kr_model_version": FEATURE_VERSION,
                "us_model_version": FEATURE_VERSION,
                "feature_version": FEATURE_VERSION,
            },
            "jobs": {
                "queue_lag_seconds": 0,
                "failed_jobs_24h": failed_jobs_24h,
                "last_reconcile": row_to_dict(last_reconcile),
            },
        }


@app.post("/v1/commands/arm")
def arm(
    body: ArmRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return command_arm(
                    conn,
                    settings=settings,
                    target_mode=body.target_mode,
                    reason=body.reason,
                    expected_state_version=body.expected_state_version,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/commands/halt")
def halt(
    body: CommandRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return command_halt(
                    conn,
                    settings=settings,
                    reason=body.reason,
                    expected_state_version=body.expected_state_version,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/commands/kill")
def kill(
    body: KillRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return command_kill(
                    conn,
                    settings=settings,
                    mode=body.mode,
                    reason=body.reason,
                    expected_state_version=body.expected_state_version,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/commands/unlock")
def unlock(
    body: UnlockRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return command_unlock(
                    conn,
                    settings=settings,
                    reason=body.reason,
                    expected_state_version=body.expected_state_version,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                    confirmation_phrase=body.confirmation_phrase,
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/order-intents")
def create_intent(
    body: CreateOrderIntentRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return create_order_intent(
                    conn,
                    settings=settings,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                    symbol=body.symbol,
                    side=body.side,
                    quantity=body.quantity,
                    limit_price=body.limit_price,
                    currency=body.currency,
                    approved=body.approved,
                    priority=body.priority,
                    approval_minutes=body.approval_minutes,
                    portfolio_hash=body.portfolio_hash,
                    max_notional=body.max_notional,
                    max_slippage_bps=body.max_slippage_bps,
                )
    except MspError as exc:
        raise _handle_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/order-intents/{intent_id}/approve")
def approve_intent(
    intent_id: str,
    body: ApproveOrderIntentRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return approve_order_intent(
                    conn,
                    intent_id=intent_id,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                    max_notional=body.max_notional,
                    max_slippage_bps=body.max_slippage_bps,
                    expires_minutes=body.expires_minutes,
                    portfolio_hash=body.portfolio_hash,
                    reason=body.reason,
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.get("/v1/orders")
def orders(limit: int = 100) -> list[dict]:
    with connect(settings) as conn:
        return list_order_intents(conn, limit=limit)


@app.get("/v1/research-scores")
def research_scores(as_of_date: str | None = None, limit: int = 100) -> list[dict]:
    with connect(settings) as conn:
        return list_scores(conn, as_of_date=as_of_date, limit=limit)


@app.get("/v1/rebalances")
def rebalances(limit: int = 20) -> list[dict]:
    with connect(settings) as conn:
        return list_rebalances(conn, limit=limit)


@app.post("/v1/rebalances")
def create_rebalance(body: GenerateRebalanceRequest) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return generate_rebalance(
                    conn,
                    account_id=settings.account_id,
                    as_of_date=body.as_of_date,
                    top_n=body.top_n,
                    gross_exposure=body.gross_exposure,
                    actor="api",
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/rebalances/{rebalance_id}/approve")
def approve_rebalance_endpoint(
    rebalance_id: str,
    body: ApproveRebalanceRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return approve_rebalance(
                    conn,
                    settings=settings,
                    rebalance_id=rebalance_id,
                    portfolio_hash=body.portfolio_hash,
                    max_notional=body.max_notional,
                    max_slippage_bps=body.max_slippage_bps,
                    idempotency_key=_idempotency(idempotency_key, body.idempotency_key),
                    actor="api",
                    create_orders=body.create_orders,
                )
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.get("/v1/positions")
def positions() -> dict:
    with connect(settings) as conn:
        broker = PaperBrokerAdapter(conn)
        return {
            "cash": broker.list_cash(settings.account_id),
            "positions": broker.list_positions(settings.account_id),
        }


@app.get("/v1/audit-events")
def audit_events(limit: int = 100) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        events = []
        for row in rows:
            event = row_to_dict(row) or {}
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        return events


@app.post("/v1/demo/seed-cash")
def seed_cash(body: SeedCashRequest) -> dict:
    with connect(settings) as conn:
        with transaction(conn):
            broker = PaperBrokerAdapter(conn)
            broker.seed_cash(settings.account_id, body.currency.upper(), body.amount)
            append_audit(
                conn,
                "paper_cash.seeded",
                "api",
                {"account_id": settings.account_id, "currency": body.currency.upper(), "amount": body.amount},
            )
            return {"account_id": settings.account_id, "currency": body.currency.upper(), "balance": body.amount}


@app.post("/v1/demo/run-execution-once")
def demo_run_execution_once() -> dict:
    return run_execution_once(settings=settings, owner="api-demo")


@app.post("/v1/demo/run-reconcile-once")
def demo_run_reconcile_once() -> dict:
    return run_reconcile_once(settings=settings)


@app.post("/v1/demo/run-data-once")
def demo_run_data_once() -> dict:
    with connect(settings) as conn:
        with transaction(conn):
            return seed_demo_market_data(conn, actor="api")


@app.post("/v1/demo/run-research-once")
def demo_run_research_once() -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return compute_demo_scores(conn, actor="api")
    except MspError as exc:
        raise _handle_error(exc) from exc


@app.post("/v1/demo/run-portfolio-once")
def demo_run_portfolio_once() -> dict:
    try:
        with connect(settings) as conn:
            with transaction(conn):
                return generate_rebalance(conn, account_id=settings.account_id, actor="api")
    except MspError as exc:
        raise _handle_error(exc) from exc
