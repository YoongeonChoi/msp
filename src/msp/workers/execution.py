from __future__ import annotations

import json
import os
import socket
import time

from msp.adapters.factory import broker_for_execution
from msp.db import connect, init_db, transaction
from msp.domain.enums import SystemMode
from msp.services.audit import append_audit
from msp.services.leases import acquire_execution_lease
from msp.services.orders import (
    claim_next_ready_order,
    command_from_intent,
    complete_intent_from_broker_result,
    reset_submitting_to_ready,
)
from msp.services.system_state import get_system_state
from msp.settings import Settings, load_settings


def default_owner() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def run_once(settings: Settings | None = None, owner: str | None = None) -> dict:
    settings = settings or load_settings()
    owner = owner or default_owner()
    init_db(settings)

    with connect(settings) as conn:
        with transaction(conn):
            lease = acquire_execution_lease(
                conn,
                account_id=settings.account_id,
                owner=owner,
                ttl_seconds=settings.lease_ttl_seconds,
            )
            if lease is None:
                return {"executed": 0, "reason": "lease_unavailable"}

            state = get_system_state(conn)
            if state["mode"] not in {SystemMode.PAPER.value, SystemMode.ARMED.value, SystemMode.LIVE.value}:
                return {"executed": 0, "reason": f"mode_{state['mode'].lower()}"}
            if state["mode"] == SystemMode.LIVE.value and not settings.allow_live_mode:
                return {"executed": 0, "reason": "live_mode_disabled"}
            if (
                state["mode"] == SystemMode.LIVE.value
                and settings.broker_adapter == "toss"
                and not settings.enable_real_broker
            ):
                return {"executed": 0, "reason": "real_broker_disabled"}

            intent = claim_next_ready_order(
                conn,
                account_id=settings.account_id,
                owner=owner,
                fencing_token=lease["fencing_token"],
            )
            if intent is None:
                return {"executed": 0, "reason": "no_ready_order"}

    with connect(settings) as conn:
        with transaction(conn):
            state = get_system_state(conn)
            if state["mode"] not in {SystemMode.PAPER.value, SystemMode.ARMED.value, SystemMode.LIVE.value}:
                reset_submitting_to_ready(conn, intent_id=intent["id"], reason="state changed before broker submit")
                return {"executed": 0, "reason": f"state_changed_to_{state['mode'].lower()}"}

            broker = broker_for_execution(conn, settings, state["mode"])
            result = broker.submit_order(command_from_intent(intent))
            updated = complete_intent_from_broker_result(
                conn,
                intent=intent,
                result=result,
                actor=owner,
            )
            append_audit(
                conn,
                "execution.order_processed",
                owner,
                {
                    "order_intent_id": intent["id"],
                    "broker_order_id": result.broker_order_id,
                    "status": updated["status"],
                },
            )
            return {"executed": 1, "order_intent": updated}


def main() -> None:
    settings = load_settings()
    owner = default_owner()
    init_db(settings)
    print(f"execution worker started owner={owner}")
    try:
        while True:
            result = run_once(settings=settings, owner=owner)
            if result.get("executed"):
                print(json.dumps(result, sort_keys=True))
            time.sleep(settings.worker_interval_seconds)
    except KeyboardInterrupt:
        print("execution worker stopped")


if __name__ == "__main__":
    main()
