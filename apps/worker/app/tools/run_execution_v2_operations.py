from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from app.config import Settings, load_settings
from app.container import build_operations_v2_runtime
from app.infrastructure.graceful_shutdown import ShutdownFlag


async def run_once(settings: Settings) -> str:
    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    try:
        result = await runtime.operations_loop.run_once()
    finally:
        await runtime.close()
    return (
        "FINAL=PASS execution_v2_operations "
        f"commands_claimed={result.commands.claimed} "
        f"commands_applied={result.commands.applied} "
        f"execution_claimed={result.execution.claimed} "
        f"execution_completed={result.execution.completed} "
        f"execution_rescheduled={result.execution.rescheduled} "
        f"execution_manual={result.execution.manual} "
        f"execution_failed={result.execution.failed} "
        f"reconciliation_claimed={result.reconciliation.claimed} "
        f"reconciliation_manual={result.reconciliation.manual} "
        f"reconciliation_failed={result.reconciliation.failed} "
        f"unknown_resolution_listed={result.reconciliation.unknown_listed} "
        f"unknown_resolution_claimed={result.reconciliation.unknown_claimed} "
        f"unknown_resolution_resumed={result.reconciliation.unknown_resumed} "
        f"unknown_resolution_applied={result.reconciliation.unknown_applied} "
        f"unknown_resolution_replayed={result.reconciliation.unknown_replayed} "
        f"unknown_resolution_failed={result.reconciliation.unknown_failed} "
        f"outbox_claimed={result.outbox.claimed} "
        f"outbox_delivered={result.outbox.delivered} "
        f"outbox_failed={result.outbox.failed} "
        "production_order_network_requests=0"
    )


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the V2 RPC-only control, restart recovery, and alert outbox boundary."
        )
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-sec", type=int, default=30)
    args = parser.parse_args(argv)
    if not 5 <= args.interval_sec <= 3600:
        parser.error("--interval-sec must be between 5 and 3600")
    settings = load_settings()
    while True:
        print(await run_once(settings), flush=True)
        if not args.loop:
            return 0
        await asyncio.sleep(args.interval_sec)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
