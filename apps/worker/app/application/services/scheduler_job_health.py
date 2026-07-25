from __future__ import annotations

from typing import Literal

from app.domain.scheduler.models import SchedulerInvariantError, SchedulerJobKey

SchedulerJobHealth = Literal["ok", "warning", "error"]

_JOB_ERROR_COUNTERS: dict[SchedulerJobKey, tuple[str, ...]] = {
    "operations.commands": ("failed", "unacknowledged"),
    "operations.execution": ("failed",),
    "operations.settlement": ("failed", "dead_lettered"),
    "operations.reconciliation": ("failed", "unknown_failed"),
    "operations.outbox": ("failed",),
}
_JOB_WARNING_COUNTERS: dict[SchedulerJobKey, tuple[str, ...]] = {
    "operations.commands": (),
    "operations.execution": ("blocked", "manual"),
    "operations.settlement": (),
    "operations.reconciliation": ("manual",),
    "operations.outbox": (),
}


def classify_successful_scheduler_job(
    job_key: SchedulerJobKey,
    handler_result: object,
) -> SchedulerJobHealth:
    """Map a validated handler result to operator-visible health.

    Scheduler settlement success means the handler result was durably recorded;
    it does not mean that every business item completed without a block, manual
    review, or failure.  Those counters remain visible in worker health.
    """

    try:
        error_fields = _JOB_ERROR_COUNTERS[job_key]
        warning_fields = _JOB_WARNING_COUNTERS[job_key]
    except KeyError:
        raise SchedulerInvariantError("scheduler_handler_health_job_is_invalid") from None
    error = any(
        _result_counter(handler_result, field_name) > 0
        for field_name in error_fields
    )
    warning = any(
        _result_counter(handler_result, field_name) > 0
        for field_name in warning_fields
    )
    return "error" if error else "warning" if warning else "ok"


def _result_counter(result: object, field_name: str) -> int:
    value = getattr(result, field_name, None)
    if type(value) is not int or value < 0:
        raise SchedulerInvariantError("scheduler_handler_health_result_is_invalid")
    return value


__all__ = ("SchedulerJobHealth", "classify_successful_scheduler_job")
