from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.services.scheduler_job_health import (
    classify_successful_scheduler_job,
)
from app.domain.scheduler.models import SchedulerInvariantError, SchedulerJobKey


def _result(**overrides: int) -> SimpleNamespace:
    values = {
        "failed": 0,
        "unacknowledged": 0,
        "blocked": 0,
        "manual": 0,
        "dead_lettered": 0,
        "unknown_failed": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "job_key",
    (
        "operations.commands",
        "operations.execution",
        "operations.settlement",
        "operations.reconciliation",
        "operations.outbox",
    ),
)
def test_clean_handler_results_are_healthy(job_key: SchedulerJobKey) -> None:
    assert classify_successful_scheduler_job(job_key, _result()) == "ok"


@pytest.mark.parametrize(
    ("job_key", "counters"),
    (
        ("operations.commands", {"unacknowledged": 1}),
        ("operations.execution", {"failed": 1}),
        ("operations.settlement", {"dead_lettered": 1}),
        ("operations.reconciliation", {"unknown_failed": 1}),
        ("operations.outbox", {"failed": 1}),
    ),
)
def test_business_failures_remain_operator_visible(
    job_key: SchedulerJobKey,
    counters: dict[str, int],
) -> None:
    assert classify_successful_scheduler_job(job_key, _result(**counters)) == "error"


@pytest.mark.parametrize(
    ("job_key", "counters"),
    (
        ("operations.execution", {"blocked": 1}),
        ("operations.execution", {"manual": 1}),
        ("operations.reconciliation", {"manual": 1}),
    ),
)
def test_manual_or_blocked_work_is_a_warning(
    job_key: SchedulerJobKey,
    counters: dict[str, int],
) -> None:
    assert classify_successful_scheduler_job(job_key, _result(**counters)) == "warning"


def test_health_classifier_rejects_missing_or_boolean_counters() -> None:
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_handler_health_result_is_invalid",
    ):
        classify_successful_scheduler_job("operations.commands", SimpleNamespace())

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_handler_health_result_is_invalid",
    ):
        classify_successful_scheduler_job(
            "operations.outbox",
            _result(failed=True),
        )
