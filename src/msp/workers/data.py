from __future__ import annotations

import json
import time

from msp.db import connect, init_db, transaction
from msp.services.data_platform import seed_demo_market_data
from msp.settings import Settings, load_settings


def run_once(settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            return seed_demo_market_data(conn)


def main() -> None:
    settings = load_settings()
    init_db(settings)
    print("data worker started")
    try:
        while True:
            result = run_once(settings=settings)
            print(json.dumps(result, sort_keys=True))
            time.sleep(settings.worker_interval_seconds * 60)
    except KeyboardInterrupt:
        print("data worker stopped")


if __name__ == "__main__":
    main()
