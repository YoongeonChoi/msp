from __future__ import annotations

import json
import time

from msp.adapters.factory import broker_for_reconciliation
from msp.db import connect, init_db, transaction
from msp.services.reconcile import reconcile_account
from msp.settings import Settings, load_settings


def run_once(settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            broker = broker_for_reconciliation(conn, settings)
            return reconcile_account(conn, account_id=settings.account_id, broker=broker)


def main() -> None:
    settings = load_settings()
    init_db(settings)
    print("reconcile worker started")
    try:
        while True:
            result = run_once(settings=settings)
            if result.get("status") != "OK":
                print(json.dumps(result, sort_keys=True))
            time.sleep(settings.worker_interval_seconds * 5)
    except KeyboardInterrupt:
        print("reconcile worker stopped")


if __name__ == "__main__":
    main()
