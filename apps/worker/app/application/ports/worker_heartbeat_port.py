from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.common.json import JsonObject
from app.domain.operations.models import RecordedWorkerHeartbeat, WorkerHeartbeatStatus


class WorkerHeartbeatPort(Protocol):
    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> RecordedWorkerHeartbeat:
        ...
