from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.common.json import JsonObject

BatchJobStatus = Literal["validating", "in_progress", "completed", "failed", "expired", "cancelled"]


@dataclass(frozen=True, slots=True)
class MonthlyCandidateBatchRequest:
    month: str
    dataset_payload: JsonObject


@dataclass(frozen=True, slots=True)
class AIBatchJob:
    provider: str
    provider_job_id: str
    status: BatchJobStatus


class AIBatchPort(Protocol):
    async def submit_monthly_candidate_batch(
        self, request: MonthlyCandidateBatchRequest
    ) -> AIBatchJob:
        ...

    async def get_batch_job(self, provider_job_id: str) -> AIBatchJob:
        ...
