from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Literal

from app.application.services.durable_scheduler_loop import DurableSchedulerCycleResult
from app.application.services.scheduler_job_health import (
    classify_successful_scheduler_job,
)
from app.config import Settings, load_settings
from app.container import build_operations_v2_runtime
from app.infrastructure.graceful_shutdown import ShutdownFlag

_FinalStatus = Literal["PASS", "WAIT", "FAIL"]
_EXIT_SUCCESS = 0
_EXIT_FAILURE = 1
_EXIT_WAIT = 2


async def run_once(settings: Settings) -> str:
    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    try:
        result = await runtime.scheduler_loop.run_once()
    except BaseException as primary_failure:
        try:
            await runtime.close()
        except BaseException as cleanup_failure:
            primary_failure.add_note(
                "operations runtime cleanup also failed: "
                f"{type(cleanup_failure).__name__}"
            )
        raise
    else:
        await runtime.close()
    return _render_cycle_result(result)


def _render_cycle_result(result: DurableSchedulerCycleResult | None) -> str:
    status, reason = _classify_cycle_result(result)
    if result is None:
        convergence = "none"
        run_outcome = "none"
        job_key = "none"
    else:
        convergence = result.convergence.outcome
        run_result = result.run_result
        run_outcome = run_result.outcome if run_result is not None else "none"
        job_key = (
            run_result.job_key
            if run_result is not None and run_result.job_key is not None
            else "none"
        )

    return (
        f"FINAL={status} durable_scheduler "
        f"convergence={convergence} "
        f"run_outcome={run_outcome} "
        f"job_key={job_key} "
        f"reason={reason} "
        "production_order_network_requests=0"
    )


def _classify_cycle_result(
    result: DurableSchedulerCycleResult | None,
) -> tuple[_FinalStatus, str]:
    if result is None:
        return "WAIT", "stopped_before_claim"

    convergence = result.convergence.outcome
    if convergence == "manual_resolution":
        return "FAIL", "manual_resolution_required"
    if convergence != "converged":
        return "WAIT", f"convergence_{convergence}"

    run_result = result.run_result
    if run_result is None:
        return "WAIT", "normal_claim_not_run"
    if run_result.outcome == "dead_letter":
        return "FAIL", "dead_letter"
    if run_result.outcome == "retry_wait":
        return "WAIT", "retry_wait"
    if run_result.outcome == "idle":
        return "PASS", "idle"
    if run_result.outcome == "succeeded":
        if run_result.job_key is None:
            return "FAIL", "successful_run_job_key_missing"
        health = classify_successful_scheduler_job(
            run_result.job_key,
            run_result.handler_result,
        )
        if health == "error":
            return "FAIL", "handler_result_error"
        if health == "warning":
            return "WAIT", "handler_result_warning"
        return "PASS", "succeeded"
    return "FAIL", "invalid_run_outcome"


def _exit_code(final_output: str) -> int:
    if final_output.startswith("FINAL=PASS "):
        return _EXIT_SUCCESS
    if final_output.startswith("FINAL=WAIT "):
        return _EXIT_WAIT
    if final_output.startswith("FINAL=FAIL "):
        return _EXIT_FAILURE
    raise RuntimeError("durable_scheduler_tool_final_status_is_invalid")


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run at most one sealed durable scheduler claim.",
    )
    parser.parse_args(list(argv) if argv is not None else [])
    settings = load_settings()
    final_output = await run_once(settings)
    print(final_output, flush=True)
    return _exit_code(final_output)


def main(argv: Sequence[str] | None = None) -> int:
    resolved_argv = sys.argv[1:] if argv is None else argv
    return asyncio.run(async_main(resolved_argv))


if __name__ == "__main__":
    raise SystemExit(main())
