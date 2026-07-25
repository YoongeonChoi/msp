from __future__ import annotations

import sys
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast

import pytest

from app.application.services.durable_scheduler_loop import DurableSchedulerCycleResult
from app.config import Settings
from app.tools import run_execution_v2_operations as tool


class _StageHealthResult:
    def __init__(self, *, failed: int = 0, blocked: int = 0) -> None:
        self.failed = failed
        self.unacknowledged = 0
        self.blocked = blocked
        self.manual = 0
        self.dead_lettered = 0
        self.unknown_failed = 0


def _cycle(
    convergence: str,
    run_outcome: str | None = None,
    *,
    handler_result: object | None = None,
    job_key: str = "operations.commands",
) -> DurableSchedulerCycleResult:
    run_result = (
        None
        if run_outcome is None
        else SimpleNamespace(
            outcome=run_outcome,
            job_key=None if run_outcome == "idle" else job_key,
            handler_result=handler_result or _StageHealthResult(),
        )
    )
    return cast(
        DurableSchedulerCycleResult,
        SimpleNamespace(
            convergence=SimpleNamespace(outcome=convergence),
            run_result=run_result,
        ),
    )


class _FakeSchedulerLoop:
    def __init__(
        self,
        cycle: DurableSchedulerCycleResult,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.cycle = cycle
        self.failure = failure

    async def run_once(self) -> DurableSchedulerCycleResult:
        if self.failure is not None:
            raise self.failure
        return self.cycle


class _FakeRuntime:
    def __init__(
        self,
        cycle: DurableSchedulerCycleResult,
        *,
        scheduler_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.scheduler_loop = _FakeSchedulerLoop(cycle, failure=scheduler_failure)
        self.close_failure = close_failure
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


@pytest.mark.parametrize(
    ("cycle", "expected_status", "expected_exit"),
    [
        (None, "FINAL=WAIT", 2),
        (_cycle("wait"), "FINAL=WAIT", 2),
        (_cycle("converged", "retry_wait"), "FINAL=WAIT", 2),
        (_cycle("converged", "dead_letter"), "FINAL=FAIL", 1),
        (_cycle("converged", "unexpected"), "FINAL=FAIL", 1),
        (_cycle("manual_resolution"), "FINAL=FAIL", 1),
        (_cycle("converged", "idle"), "FINAL=PASS", 0),
        (_cycle("converged", "succeeded"), "FINAL=PASS", 0),
        (
            _cycle(
                "converged",
                "succeeded",
                handler_result=_StageHealthResult(failed=1),
            ),
            "FINAL=FAIL",
            1,
        ),
        (
            _cycle(
                "converged",
                "succeeded",
                handler_result=_StageHealthResult(blocked=1),
                job_key="operations.execution",
            ),
            "FINAL=WAIT",
            2,
        ),
    ],
)
def test_cycle_status_is_never_pass_for_incomplete_or_failed_work(
    cycle: DurableSchedulerCycleResult | None,
    expected_status: str,
    expected_exit: int,
) -> None:
    output = tool._render_cycle_result(cycle)

    assert output.startswith(expected_status)
    assert tool._exit_code(output) == expected_exit
    assert "recovery_runs=" not in output
    assert output.endswith("production_order_network_requests=0")
    if (
        cycle is not None
        and cycle.run_result is not None
        and cycle.run_result.outcome == "idle"
    ):
        assert "job_key=none" in output


async def test_run_once_closes_runtime_and_reports_only_the_observed_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime(_cycle("converged", "succeeded"))
    monkeypatch.setattr(tool, "build_operations_v2_runtime", lambda *_args: runtime)

    output = await tool.run_once(Settings())

    assert output.startswith("FINAL=PASS durable_scheduler ")
    assert "convergence=converged" in output
    assert "run_outcome=succeeded" in output
    assert "job_key=operations.commands" in output
    assert runtime.closed is True


async def test_run_once_preserves_scheduler_failure_when_close_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("scheduler_fail_stop")
    runtime = _FakeRuntime(
        _cycle("converged", "idle"),
        scheduler_failure=primary,
        close_failure=RuntimeError("close_failed"),
    )
    monkeypatch.setattr(tool, "build_operations_v2_runtime", lambda *_args: runtime)

    with pytest.raises(KeyboardInterrupt, match="scheduler_fail_stop") as raised:
        await tool.run_once(Settings())

    assert runtime.closed is True
    assert (
        "operations runtime cleanup also failed: RuntimeError"
        in getattr(raised.value, "__notes__", ())
    )


def test_main_none_forwards_the_real_process_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    async def fake_async_main(argv: Sequence[str] | None) -> int:
        assert argv is not None
        captured.append(tuple(argv))
        return 7

    monkeypatch.setattr(tool, "async_main", fake_async_main)
    monkeypatch.setattr(sys, "argv", ["durable-scheduler", "--loop", "--interval-sec", "5"])

    assert tool.main(None) == 7
    assert captured == [("--loop", "--interval-sec", "5")]


async def test_deprecated_cadence_arguments_are_rejected_before_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def settings_must_not_load() -> Settings:
        raise AssertionError("settings_must_not_load_for_invalid_cli")

    monkeypatch.setattr(tool, "load_settings", settings_must_not_load)

    with pytest.raises(SystemExit) as captured:
        await tool.async_main(("--loop", "--interval-sec", "5"))

    assert captured.value.code == 2


@pytest.mark.parametrize(
    ("output", "expected_exit"),
    [
        ("FINAL=PASS durable_scheduler reason=idle", 0),
        ("FINAL=WAIT durable_scheduler reason=retry_wait", 2),
        ("FINAL=FAIL durable_scheduler reason=dead_letter", 1),
    ],
)
async def test_async_main_returns_the_rendered_status_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output: str,
    expected_exit: int,
) -> None:
    settings = Settings()

    async def fake_run_once(received: Settings) -> str:
        assert received is settings
        return output

    monkeypatch.setattr(tool, "load_settings", lambda: settings)
    monkeypatch.setattr(tool, "run_once", fake_run_once)

    assert await tool.async_main(()) == expected_exit
    assert capsys.readouterr().out == output + "\n"
