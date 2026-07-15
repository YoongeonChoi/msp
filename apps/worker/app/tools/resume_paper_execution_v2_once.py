from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.application.use_cases.run_execution_v2 import (
    ExecutionV2RunOutcome,
    PaperExecutionV2Command,
    RunExecutionV2,
)
from app.config import Settings, load_settings
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    MinuteBar,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
)

MAX_RESUME_INPUT_BYTES = 1_000_000
MAX_RESUME_BARS = 600
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionIntentInput(_StrictInput):
    id: StrictStr
    decision_id: StrictStr
    risk_result_id: StrictStr
    decision_feature_sha256: StrictStr
    risk_allowed: StrictBool
    risk_reason_codes: tuple[StrictStr, ...]
    risk_evaluated_at: datetime
    risk_expires_at: datetime
    semantic_key: StrictStr
    account_id: StrictStr
    environment: Literal["paper"]
    strategy_version_id: StrictStr
    symbol: StrictStr
    side: Literal["buy", "sell"]
    quantity: StrictInt
    limit_price_krw: StrictInt
    decision_at: datetime
    signal_valid_from: datetime
    signal_valid_until: datetime
    execution_policy_version: StrictStr
    cost_schedule_version: StrictStr
    cost_schedule_evidence_sha256: StrictStr
    cash_commitment_krw: StrictInt
    eligible_at: datetime
    expires_at: datetime
    gate_epoch: StrictInt
    lease_holder_id: StrictStr
    lease_fencing_token: StrictInt
    time_in_force: Literal["DAY"]

    def to_domain(self) -> ExecutionIntent:
        return ExecutionIntent(**self.model_dump())


class ExecutionCostScheduleInput(_StrictInput):
    version: StrictStr
    effective_from: datetime
    effective_until: datetime
    evidence_sha256: StrictStr
    settlement_days: StrictInt
    settlement_evidence_sha256: StrictStr
    buy_commission_rate: Decimal
    sell_commission_rate: Decimal
    sell_tax_rate: Decimal

    def to_domain(self) -> ExecutionCostSchedule:
        return ExecutionCostSchedule(**self.model_dump())


class MinuteBarInput(_StrictInput):
    symbol: StrictStr
    minute: datetime
    completed_at: datetime
    as_of: datetime
    source_sha256: StrictStr
    is_complete: StrictBool
    open_krw: StrictInt
    high_krw: StrictInt
    low_krw: StrictInt
    close_krw: StrictInt
    volume: StrictInt

    def to_domain(self) -> MinuteBar:
        return MinuteBar(**self.model_dump())


class PaperExecutionEvidenceInput(_StrictInput):
    version: StrictStr
    execution_policy_version: StrictStr
    effective_from: datetime
    effective_until: datetime
    tick_rule_version: StrictStr
    tick_size_krw: StrictInt
    tick_rule_evidence_sha256: StrictStr
    volume_source: StrictStr
    volume_unit: Literal["shares"]
    volume_evidence_sha256: StrictStr
    corporate_action_status: Literal["not_required", "adjusted"]
    corporate_action_evidence_sha256: StrictStr
    market_calendar_version: StrictStr
    market_calendar_status: Literal["open_sessions_verified"]
    market_calendar_evidence_sha256: StrictStr
    open_session_dates: tuple[date, ...]

    def to_domain(self) -> PaperExecutionEvidence:
        return PaperExecutionEvidence(**self.model_dump())


class PaperPositionCostBasisInput(_StrictInput):
    symbol: StrictStr
    quantity: StrictInt
    total_cost_krw: StrictInt
    accounting_method: Literal["moving_weighted_average_v1"]

    def to_domain(self) -> PaperPositionCostBasis:
        return PaperPositionCostBasis(**self.model_dump())


class PaperResumeInput(_StrictInput):
    schema_version: Literal[1]
    intent: ExecutionIntentInput
    bars: tuple[MinuteBarInput, ...] = Field(
        min_length=1,
        max_length=MAX_RESUME_BARS,
    )
    cost_schedule: ExecutionCostScheduleInput
    execution_evidence: PaperExecutionEvidenceInput
    position_cost_basis: PaperPositionCostBasisInput | None

    def to_command(self, *, evaluated_at: datetime) -> PaperExecutionV2Command:
        intent = self.intent.to_domain()
        return PaperExecutionV2Command.create(
            intent=intent,
            bars=tuple(bar.to_domain() for bar in self.bars),
            cost_schedule=self.cost_schedule.to_domain(),
            execution_evidence=self.execution_evidence.to_domain(),
            position_cost_basis=(
                self.position_cost_basis.to_domain()
                if self.position_cost_basis is not None
                else None
            ),
            # Resume is a new fenced authorization event, not a replay of the
            # original wall-clock dispatch call. The DB accepts p_now only in
            # a narrow current-time window, so use the actual resume time.
            dispatch_at=evaluated_at,
            evaluated_at=evaluated_at,
        )


def load_resume_input(path: Path, *, expected_sha256: str) -> PaperResumeInput:
    normalized_sha256 = expected_sha256.strip().lower()
    if _SHA256_RE.fullmatch(normalized_sha256) is None:
        raise ExecutionInvariantError("paper_resume_input_sha256_is_invalid")
    try:
        raw = _read_regular_file_once(path)
    except OSError as exc:
        raise ExecutionInvariantError("paper_resume_input_file_is_unreadable") from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, normalized_sha256):
        raise ExecutionInvariantError("paper_resume_input_sha256_mismatch")
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
        )
        return PaperResumeInput.model_validate(decoded)
    except (ValueError, TypeError) as exc:
        raise ExecutionInvariantError("paper_resume_input_schema_is_invalid") from exc


async def run_once(
    settings: Settings,
    resume_input: PaperResumeInput,
    *,
    evaluated_at: datetime | None = None,
) -> ExecutionV2RunOutcome:
    if not settings.execution_v2_paper_resume_input_enabled:
        raise ExecutionInvariantError("paper_resume_input_is_not_enabled")
    worker_id = settings.execution_v2_worker_id
    if worker_id is None or resume_input.intent.lease_holder_id != worker_id:
        raise ExecutionInvariantError("paper_resume_worker_identity_mismatch")
    now = evaluated_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExecutionInvariantError("paper_resume_evaluation_time_must_be_aware")
    if now >= resume_input.intent.expires_at:
        raise ExecutionInvariantError("paper_resume_after_expiry_requires_reconciliation")
    command = resume_input.to_command(evaluated_at=now)
    worker_api = SupabaseWorkerApi(settings)
    try:
        return await RunExecutionV2(durable_port=worker_api).resume_existing_paper(
            command
        )
    finally:
        await worker_api.close()


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resume one existing durable paper partial fill from a hash-pinned "
            "evidence bundle. This command never reserves a new intent."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        resume_input = load_resume_input(
            args.input,
            expected_sha256=args.input_sha256,
        )
        outcome = await run_once(load_settings(), resume_input)
    except ExecutionInvariantError as exc:
        print(
            "FINAL=FAIL paper_execution_v2_resume "
            f"reason_code={exc.safe_message} production_order_network_requests=0",
            flush=True,
        )
        return 1
    except ValidationError:
        print(
            "FINAL=FAIL paper_execution_v2_resume "
            "reason_code=paper_resume_runtime_configuration_is_invalid "
            "production_order_network_requests=0",
            flush=True,
        )
        return 1
    final = "FAIL" if outcome.status == "quarantined" else "PASS"
    reason_code = outcome.reason_code or "none"
    print(
        f"FINAL={final} paper_execution_v2_resume "
        f"intent_id={resume_input.intent.id} status={outcome.status} "
        f"reason_code={reason_code} production_order_network_requests=0",
        flush=True,
    )
    return 1 if outcome.status == "quarantined" else 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _read_regular_file_once(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        path_stat = os.stat(path, follow_symlinks=False)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        is_reparse_point = bool(
            getattr(path_stat, "st_file_attributes", 0) & reparse_attribute
        )
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or is_reparse_point
            or not os.path.samestat(opened_stat, path_stat)
        ):
            raise ExecutionInvariantError("paper_resume_input_file_is_invalid")
        if opened_stat.st_size > MAX_RESUME_INPUT_BYTES:
            raise ExecutionInvariantError("paper_resume_input_file_is_too_large")
        chunks: list[bytes] = []
        remaining = MAX_RESUME_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RESUME_INPUT_BYTES:
            raise ExecutionInvariantError("paper_resume_input_file_is_too_large")
        return raw
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
