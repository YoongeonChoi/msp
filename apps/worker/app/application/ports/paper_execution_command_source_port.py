from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from app.domain.execution_v2.models import ExecutionInvariantError
from app.domain.risk.value_objects import RiskInput

if TYPE_CHECKING:
    from app.application.use_cases.run_execution_v2 import PaperExecutionV2Command


PaperExecutionCommandKind = Literal["new_candidate", "resume_existing"]
PaperExecutionSourceOutcome = Literal["complete", "reschedule", "manual"]
PaperExecutionSourceState = Literal["complete", "pending", "manual"]

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True)
class ClaimedPaperExecutionCommand:
    """A source lease whose token and revision form the completion CAS boundary."""

    command_id: str
    intent_id: str
    kind: PaperExecutionCommandKind
    claim_token: str
    source_revision: int
    worker_id: str
    release_sha: str
    available_at: datetime
    claimed_at: datetime
    claim_expires_at: datetime

    def __post_init__(self) -> None:
        for uuid_value, field in (
            (self.command_id, "paper_command_id"),
            (self.intent_id, "paper_command_intent_id"),
            (self.claim_token, "paper_command_claim_token"),
            (self.worker_id, "paper_command_worker_id"),
        ):
            _require_uuid(uuid_value, field)
        if self.kind not in {"new_candidate", "resume_existing"}:
            raise ExecutionInvariantError("paper_command_kind_is_invalid")
        if (
            isinstance(self.source_revision, bool)
            or not isinstance(self.source_revision, int)
            or self.source_revision <= 0
        ):
            raise ExecutionInvariantError("paper_command_source_revision_is_invalid")
        if _RELEASE_SHA_RE.fullmatch(self.release_sha) is None:
            raise ExecutionInvariantError("paper_command_release_sha_is_invalid")
        for time_value, field in (
            (self.available_at, "paper_command_available_at"),
            (self.claimed_at, "paper_command_claimed_at"),
            (self.claim_expires_at, "paper_command_claim_expires_at"),
        ):
            _require_aware(time_value, field)
        if not self.available_at <= self.claimed_at < self.claim_expires_at:
            raise ExecutionInvariantError("paper_command_claim_timeline_is_invalid")


@dataclass(frozen=True, slots=True)
class PaperExecutionCommandBundle:
    command: PaperExecutionV2Command
    risk_input: RiskInput | None


@dataclass(frozen=True, slots=True)
class PaperExecutionSourceCompletion:
    command_id: str
    state: PaperExecutionSourceState
    source_revision: int
    next_available_at: datetime | None

    def __post_init__(self) -> None:
        _require_uuid(self.command_id, "paper_completion_command_id")
        if self.state not in {"complete", "pending", "manual"}:
            raise ExecutionInvariantError("paper_completion_state_is_invalid")
        if (
            isinstance(self.source_revision, bool)
            or not isinstance(self.source_revision, int)
            or self.source_revision <= 0
        ):
            raise ExecutionInvariantError("paper_completion_source_revision_is_invalid")
        if self.next_available_at is not None:
            _require_aware(self.next_available_at, "paper_completion_next_available_at")
        if (self.state == "pending") != (self.next_available_at is not None):
            raise ExecutionInvariantError("paper_completion_next_available_at_is_invalid")


class PaperExecutionCommandSourcePort(Protocol):
    async def claim_available_paper_execution(
        self,
        *,
        worker_id: str,
        release_sha: str,
        now: datetime,
        lease_ttl: timedelta,
    ) -> ClaimedPaperExecutionCommand | None:
        ...

    async def load_claimed_paper_execution_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
    ) -> PaperExecutionCommandBundle:
        ...

    async def complete_or_reschedule_paper_execution(
        self,
        *,
        command_id: str,
        claim_token: str,
        expected_revision: int,
        worker_id: str,
        release_sha: str,
        now: datetime,
        outcome: PaperExecutionSourceOutcome,
        next_available_at: datetime | None,
        reason_code: str,
    ) -> PaperExecutionSourceCompletion:
        ...


def _require_uuid(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionInvariantError(f"{field}_is_invalid") from exc
    if str(parsed) != value:
        raise ExecutionInvariantError(f"{field}_is_invalid")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError(f"{field}_must_be_timezone_aware")
