from __future__ import annotations

import re
from typing import Never

_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class SchedulerProcessFailStop(BaseException):
    """Terminate the operations process without exposing unsafe internals."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def fail_stop_scheduler_process(reason: str) -> Never:
    """Non-returning production fail-stop used by the sealed scheduler graph."""

    if type(reason) is not str or _REASON_RE.fullmatch(reason) is None:
        reason = "scheduler_fail_stop_reason_invalid"
    raise SchedulerProcessFailStop(reason)
