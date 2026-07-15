from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class PaperBarFixtureReceipt:
    series_id: str
    fixture_set_id: str
    batch_sequence: int
    bar_count: int
    fixture_sha256: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class PaperCandidateReceipt:
    command_id: str
    intent_id: str
    state: str
    source_revision: int
    semantic_key_sha256: str
    idempotent: bool


class PaperExecutionProducerPort(Protocol):
    async def ingest_bar_fixture(
        self,
        fixture: JsonObject,
        *,
        worker_id: str,
        now: datetime,
    ) -> PaperBarFixtureReceipt:
        ...

    async def enqueue_candidate(
        self,
        candidate: JsonObject,
        *,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        now: datetime,
    ) -> PaperCandidateReceipt:
        ...
