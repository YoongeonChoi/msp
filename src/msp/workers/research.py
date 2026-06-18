from __future__ import annotations

import json
import time

from msp.db import connect, init_db, transaction
from msp.services.research import compute_demo_scores
from msp.settings import Settings, load_settings


def run_once(settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    init_db(settings)
    with connect(settings) as conn:
        with transaction(conn):
            return compute_demo_scores(conn)


def main() -> None:
    settings = load_settings()
    init_db(settings)
    print("research worker started")
    try:
        while True:
            result = run_once(settings=settings)
            print(json.dumps(result, sort_keys=True))
            time.sleep(settings.worker_interval_seconds * 60)
    except KeyboardInterrupt:
        print("research worker stopped")


if __name__ == "__main__":
    main()
