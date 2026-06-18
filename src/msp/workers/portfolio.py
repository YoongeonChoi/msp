from __future__ import annotations

import json
import time

from msp.db import connect, init_db, transaction
from msp.services.portfolio import generate_rebalance
from msp.settings import Settings, load_settings


def run_once(settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            return generate_rebalance(conn, account_id=settings.account_id)


def main() -> None:
    settings = load_settings()
    init_db(settings)
    print("portfolio worker started")
    try:
        while True:
            result = run_once(settings=settings)
            print(json.dumps({"rebalance_id": result["id"], "status": result["status"]}, sort_keys=True))
            time.sleep(settings.worker_interval_seconds * 300)
    except KeyboardInterrupt:
        print("portfolio worker stopped")


if __name__ == "__main__":
    main()
