from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from app.application.ports.execution_kernel_port import (
    DurableExecutionV2Port,
    ExecutionKernelPort,
)
from app.application.services.paper_execution_v2 import DeterministicPaperExecutionSimulator
from app.domain.execution_v2.models import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    MinuteBar,
    PaperExecutionCheckpoint,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
    PaperSimulationResult,
    build_observation_history_sha256,
    new_accounting_transaction_id,
)


@dataclass(frozen=True, slots=True)
class PaperExecutionV2Command:
    intent: ExecutionIntent
    bars: tuple[MinuteBar, ...]
    cost_schedule: ExecutionCostSchedule
    execution_evidence: PaperExecutionEvidence
    position_cost_basis: PaperPositionCostBasis | None
    dispatch_at: datetime
    evaluated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        intent: ExecutionIntent,
        bars: Sequence[MinuteBar],
        cost_schedule: ExecutionCostSchedule,
        execution_evidence: PaperExecutionEvidence,
        position_cost_basis: PaperPositionCostBasis | None = None,
        dispatch_at: datetime,
        evaluated_at: datetime,
    ) -> PaperExecutionV2Command:
        if intent.environment != "paper":
            raise ExecutionInvariantError("paper_v2_command_requires_paper_intent")
        if dispatch_at.tzinfo is None or dispatch_at.utcoffset() is None:
            raise ExecutionInvariantError("paper_v2_dispatch_time_must_be_aware")
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ExecutionInvariantError("paper_v2_evaluation_time_must_be_aware")
        if not intent.eligible_at <= dispatch_at <= intent.expires_at:
            raise ExecutionInvariantError("paper_v2_dispatch_outside_execution_window")
        if evaluated_at < dispatch_at:
            raise ExecutionInvariantError("paper_v2_evaluation_precedes_dispatch")
        if intent.side == "sell" and (
            position_cost_basis is None
            or position_cost_basis.symbol != intent.symbol
            or position_cost_basis.quantity < intent.quantity
        ):
            raise ExecutionInvariantError(
                "paper_v2_sell_position_cost_basis_is_required"
            )
        if intent.side == "buy" and position_cost_basis is not None:
            raise ExecutionInvariantError(
                "paper_v2_buy_position_cost_basis_is_unexpected"
            )
        return cls(
            intent=intent,
            bars=tuple(bars),
            cost_schedule=cost_schedule,
            execution_evidence=execution_evidence,
            position_cost_basis=position_cost_basis,
            dispatch_at=dispatch_at,
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class ExecutionV2RunOutcome:
    status: Literal[
        "completed",
        "pending",
        "duplicate_semantic_intent",
        "quarantined",
    ]
    result: PaperSimulationResult | None
    reason_code: str | None = None


class RunExecutionV2:
    """Explicit V2 orchestration; it never calls the legacy repository order API."""

    def __init__(
        self,
        kernel: ExecutionKernelPort | None = None,
        *,
        durable_port: DurableExecutionV2Port | None = None,
        simulator: DeterministicPaperExecutionSimulator | None = None,
    ) -> None:
        if kernel is None and durable_port is None:
            raise ExecutionInvariantError("execution_v2_requires_execution_port")
        self.kernel = kernel
        self.durable_port = durable_port
        self.simulator = simulator or DeterministicPaperExecutionSimulator()

    async def execute_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        if self.durable_port is None:
            return await self._execute_in_memory(command)
        return await self._execute_durable(command)

    async def resume_existing_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        """Resume only an already-reserved durable paper intent.

        Unlike ``execute_paper``, this bounded entrypoint never calls the
        reservation RPC and therefore cannot create a new intent.
        """

        if self.durable_port is None:
            raise ExecutionInvariantError("paper_resume_requires_durable_port")
        checkpoint = await self.durable_port.load_paper_execution_checkpoint(
            command.intent,
            now=command.evaluated_at,
        )
        if checkpoint.is_terminal:
            return ExecutionV2RunOutcome(
                "completed",
                None,
                reason_code="paper_execution_already_terminal",
            )
        if checkpoint.latest_status not in {"open", "partial_filled"}:
            raise ExecutionInvariantError(
                "paper_resume_requires_persisted_open_or_partial"
            )
        await self.durable_port.mark_dispatch_started(
            command.intent,
            now=command.evaluated_at,
        )
        return await self._simulate_and_record_durable(
            command,
            command.intent,
            checkpoint,
        )

    async def _execute_in_memory(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        if self.kernel is None:
            raise ExecutionInvariantError("paper_execution_requires_in_memory_kernel")
        reserved = await self.kernel.reserve_intent(
            command.intent,
            now=command.intent.decision_at,
        )
        if not reserved:
            return ExecutionV2RunOutcome("duplicate_semantic_intent", None)
        await self.kernel.mark_dispatch_started(
            command.intent,
            now=command.dispatch_at,
        )
        result = await self.kernel.execute_paper(
            command.intent,
            command.bars,
            cost_schedule=command.cost_schedule,
            execution_evidence=command.execution_evidence,
            now=command.evaluated_at,
            position_cost_basis=command.position_cost_basis,
        )
        return ExecutionV2RunOutcome(_paper_result_status(result), result)

    async def _execute_durable(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        assert self.durable_port is not None
        reservation = await self.durable_port.reserve_order_intent(command.intent)
        if reservation.state == "semantic_duplicate":
            if reservation.intent_id == command.intent.id:
                raise ExecutionInvariantError(
                    "durable_semantic_duplicate_identity_is_invalid"
                )
            return ExecutionV2RunOutcome(
                "duplicate_semantic_intent",
                None,
                reason_code=reservation.reason_code,
            )
        if reservation.state == "created" and reservation.intent_id != command.intent.id:
            raise ExecutionInvariantError("durable_reservation_intent_mismatch")
        if (
            reservation.state == "existing_replay"
            and reservation.intent_id != command.intent.id
        ):
            raise ExecutionInvariantError("durable_replay_intent_mismatch")
        persisted_intent = command.intent
        await self.durable_port.mark_dispatch_started(
            persisted_intent,
            now=(
                command.dispatch_at
                if reservation.state == "created"
                else command.evaluated_at
            ),
        )
        checkpoint = (
            None
            if reservation.state == "created"
            else await self.durable_port.load_paper_execution_checkpoint(
                persisted_intent,
                now=command.evaluated_at,
            )
        )
        return await self._simulate_and_record_durable(
            command,
            persisted_intent,
            checkpoint,
        )

    async def _simulate_and_record_durable(
        self,
        command: PaperExecutionV2Command,
        persisted_intent: ExecutionIntent,
        checkpoint: PaperExecutionCheckpoint | None,
    ) -> ExecutionV2RunOutcome:
        assert self.durable_port is not None
        if checkpoint is not None and checkpoint.is_terminal:
            return ExecutionV2RunOutcome(
                "completed",
                None,
                reason_code="paper_execution_already_terminal",
            )
        position_cost_basis = _position_cost_basis_for_durable_execution(
            persisted_intent,
            checkpoint,
            command.position_cost_basis,
        )
        result = self.simulator.simulate(
            persisted_intent,
            command.bars,
            cost_schedule=command.cost_schedule,
            execution_evidence=command.execution_evidence,
            now=command.evaluated_at,
            position_cost_basis=position_cost_basis,
        )
        prefix_length = 0
        if checkpoint is not None:
            aligned = _align_result_with_checkpoint(
                result,
                persisted_intent,
                checkpoint,
            )
            if aligned is None:
                return ExecutionV2RunOutcome(
                    "quarantined",
                    result,
                    reason_code="paper_execution_resume_history_mismatch",
                )
            result, prefix_length = aligned
        transactions_by_sequence = {
            transaction.observation_sequence: transaction
            for transaction in result.accounting_transactions
        }
        intent_release_sha = (
            checkpoint.intent_release_sha
            if checkpoint is not None
            else self.durable_port.current_release_sha
        )
        for observation in result.observations[prefix_length:]:
            record_result = await self.durable_port.record_execution_observation(
                persisted_intent,
                observation,
                accounting_transaction=transactions_by_sequence.get(observation.sequence),
                intent_release_sha=intent_release_sha,
                now=command.evaluated_at,
            )
            if record_result.quarantined:
                return ExecutionV2RunOutcome(
                    "quarantined",
                    result,
                    reason_code=record_result.reason_code,
                )
            if observation.status == "unknown_requires_manual_check":
                return ExecutionV2RunOutcome(
                    "quarantined",
                    result,
                    reason_code=observation.reason or "unknown_requires_manual_check",
                )
        return ExecutionV2RunOutcome(_paper_result_status(result), result)


def _paper_result_status(
    result: PaperSimulationResult,
) -> Literal["completed", "pending"]:
    if (
        result.observations
        and result.observations[-1].status in TERMINAL_EXECUTION_STATUSES
    ):
        return "completed"
    return "pending"


def _position_cost_basis_for_durable_execution(
    intent: ExecutionIntent,
    checkpoint: PaperExecutionCheckpoint | None,
    supplied: PaperPositionCostBasis | None,
) -> PaperPositionCostBasis | None:
    if checkpoint is None:
        return supplied
    pinned = checkpoint.pinned_position_cost_basis(intent.symbol)
    if intent.side == "sell" and pinned is None:
        raise ExecutionInvariantError("paper_resume_sell_cost_basis_is_missing")
    if intent.side == "sell" and supplied != pinned:
        raise ExecutionInvariantError("paper_resume_sell_cost_basis_mismatch")
    if intent.side == "buy" and pinned is not None:
        raise ExecutionInvariantError("paper_resume_buy_cost_basis_is_unexpected")
    return pinned


def _paper_prefix_matches_checkpoint(
    result: PaperSimulationResult,
    checkpoint: PaperExecutionCheckpoint,
) -> bool:
    if checkpoint.latest_sequence is None:
        return True
    prefix_length = checkpoint.latest_sequence
    if len(result.observations) < prefix_length:
        return False
    prefix = result.observations[:prefix_length]
    latest = prefix[-1]
    return bool(
        build_observation_history_sha256(prefix)
        == checkpoint.observation_history_sha256
        and latest.status == checkpoint.latest_status
        and latest.observed_at == checkpoint.latest_observed_at
        and latest.cumulative_quantity == checkpoint.cumulative_quantity
        and latest.cumulative_gross_krw == checkpoint.cumulative_gross_krw
        and latest.cumulative_commission_krw
        == checkpoint.cumulative_commission_krw
        and latest.cumulative_tax_krw == checkpoint.cumulative_tax_krw
    )


def _align_result_with_checkpoint(
    result: PaperSimulationResult,
    intent: ExecutionIntent,
    checkpoint: PaperExecutionCheckpoint,
) -> tuple[PaperSimulationResult, int] | None:
    if checkpoint.latest_sequence is None:
        return result, 0
    if checkpoint.latest_status == "open":
        if any(
            value != 0
            for value in (
                checkpoint.cumulative_quantity,
                checkpoint.cumulative_gross_krw,
                checkpoint.cumulative_commission_krw,
                checkpoint.cumulative_tax_krw,
            )
        ):
            return None
        return (
            _resequence_result(
                result,
                intent,
                offset=checkpoint.latest_sequence,
            ),
            0,
        )
    if checkpoint.latest_status == "partial_filled":
        matches = tuple(
            observation
            for observation in result.observations
            if _observation_matches_checkpoint(observation, checkpoint)
        )
        if len(matches) != 1:
            return None
        matched = matches[0]
        offset = checkpoint.latest_sequence - matched.sequence
        if offset < 0:
            return None
        if offset == 0:
            if not _paper_prefix_matches_checkpoint(result, checkpoint):
                return None
            return result, matched.sequence
        return (
            _resequence_result(result, intent, offset=offset),
            matched.sequence,
        )
    if not _paper_prefix_matches_checkpoint(result, checkpoint):
        return None
    return result, checkpoint.latest_sequence


def _observation_matches_checkpoint(
    observation: ExecutionObservation,
    checkpoint: PaperExecutionCheckpoint,
) -> bool:
    return bool(
        observation.status == checkpoint.latest_status
        and observation.observed_at == checkpoint.latest_observed_at
        and observation.cumulative_quantity == checkpoint.cumulative_quantity
        and observation.cumulative_gross_krw == checkpoint.cumulative_gross_krw
        and observation.cumulative_commission_krw
        == checkpoint.cumulative_commission_krw
        and observation.cumulative_tax_krw == checkpoint.cumulative_tax_krw
    )


def _resequence_result(
    result: PaperSimulationResult,
    intent: ExecutionIntent,
    *,
    offset: int,
) -> PaperSimulationResult:
    if offset <= 0:
        raise ExecutionInvariantError("paper_resume_sequence_offset_is_invalid")
    fills = tuple(
        replace(fill, sequence=fill.sequence + offset)
        for fill in result.fills
    )
    observations: list[ExecutionObservation] = []
    for observation in result.observations:
        sequence = observation.sequence + offset
        provider_execution_id = observation.provider_execution_id
        if provider_execution_id is not None:
            expected = f"paper:{intent.id}:fill:{observation.sequence}"
            if provider_execution_id != expected:
                raise ExecutionInvariantError(
                    "paper_resume_provider_execution_identity_mismatch"
                )
            provider_execution_id = f"paper:{intent.id}:fill:{sequence}"
        observations.append(
            ExecutionObservation.create(
                intent_id=observation.intent_id,
                sequence=sequence,
                status=observation.status,
                observed_at=observation.observed_at,
                provider_order_id=observation.provider_order_id,
                provider_execution_id=provider_execution_id,
                cumulative_quantity=observation.cumulative_quantity,
                cumulative_gross_krw=observation.cumulative_gross_krw,
                cumulative_commission_krw=observation.cumulative_commission_krw,
                cumulative_tax_krw=observation.cumulative_tax_krw,
                last_fill_quantity=observation.last_fill_quantity,
                last_fill_price_krw=observation.last_fill_price_krw,
                last_fill_settlement_date=observation.last_fill_settlement_date,
                reason=observation.reason,
            )
        )
    transactions = tuple(
        replace(
            transaction,
            id=new_accounting_transaction_id(
                intent.id,
                transaction.observation_sequence + offset,
            ),
            observation_sequence=transaction.observation_sequence + offset,
        )
        for transaction in result.accounting_transactions
    )
    return PaperSimulationResult(
        fills=fills,
        observations=tuple(observations),
        accounting_transactions=transactions,
    )
