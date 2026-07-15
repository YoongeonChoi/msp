from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.application.services.paper_execution_v2 import (
    DeterministicPaperExecutionSimulator,
    build_fill_accounting_transaction,
)
from app.domain.execution_v2.models import (
    BLOCKING_EXECUTION_STATUSES,
    TERMINAL_EXECUTION_STATUSES,
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionEnvironment,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    MinuteBar,
    PaperAccountSnapshot,
    PaperExecutionEvidence,
    PaperFill,
    PaperPositionCostBasis,
    PaperPositionSnapshot,
    PaperSimulationResult,
    WorkerLease,
    validate_execution_environment,
)


@dataclass(slots=True)
class _MutablePosition:
    quantity: int
    total_cost_krw: int


@dataclass(slots=True)
class _MutablePaperAccount:
    cash_krw: int
    reserved_cash_krw: int
    positions: dict[str, _MutablePosition]
    reserved_position_quantities: dict[str, int]


@dataclass(slots=True)
class _IntentReservation:
    account_id: str
    symbol: str
    side: str
    remaining_cash_krw: int
    remaining_quantity: int
    terminal: bool = False


class InMemoryExecutionKernelV2:
    """Concurrency-safe reference kernel with fenced, atomic resource reservations."""

    def __init__(
        self,
        environment: ExecutionEnvironment,
        *,
        paper_simulator: DeterministicPaperExecutionSimulator | None = None,
    ) -> None:
        self.environment = validate_execution_environment(environment)
        self._paper_simulator = paper_simulator or DeterministicPaperExecutionSimulator()
        self._lock = asyncio.Lock()
        self._accounts: dict[str, _MutablePaperAccount] = {}
        self._gates: dict[str, ExecutionGate] = {}
        self._leases: dict[str, WorkerLease] = {}
        self._last_fencing_tokens: dict[str, int] = {}
        self._intents_by_id: dict[str, ExecutionIntent] = {}
        self._intent_ids_by_semantic_key: dict[str, str] = {}
        self._reservations_by_intent: dict[str, _IntentReservation] = {}
        self._observations_by_intent: dict[str, tuple[ExecutionObservation, ...]] = {}
        self._transactions_by_id: dict[str, AccountingTransaction] = {}
        self._provider_execution_bindings: dict[str, tuple[str, int, str]] = {}
        self._results_by_intent: dict[str, PaperSimulationResult] = {}
        self._position_cost_basis_by_intent: dict[
            str,
            PaperPositionCostBasis | None,
        ] = {}
        self._dispatch_started: set[str] = set()

    async def configure_account(
        self,
        account_id: str,
        cash_krw: int,
        positions: Sequence[PaperPositionCostBasis] | None = None,
    ) -> None:
        if not account_id.strip():
            raise ExecutionInvariantError("account_id_must_be_nonempty")
        if isinstance(cash_krw, bool) or not isinstance(cash_krw, int) or cash_krw < 0:
            raise ExecutionInvariantError("cash_krw_must_be_nonnegative_integer")
        normalized_positions: dict[str, _MutablePosition] = {}
        for position in positions or ():
            if position.symbol in normalized_positions:
                raise ExecutionInvariantError("duplicate_configured_position_symbol")
            normalized_positions[position.symbol] = _MutablePosition(
                quantity=position.quantity,
                total_cost_krw=position.total_cost_krw,
            )
        async with self._lock:
            if any(intent.account_id == account_id for intent in self._intents_by_id.values()):
                raise ExecutionInvariantError("cannot_reconfigure_account_after_intent_reservation")
            self._accounts[account_id] = _MutablePaperAccount(
                cash_krw=cash_krw,
                reserved_cash_krw=0,
                positions=normalized_positions,
                reserved_position_quantities={},
            )

    async def replace_gate(self, gate: ExecutionGate) -> None:
        if gate.environment != self.environment:
            raise ExecutionInvariantError("execution_gate_environment_mismatch")
        async with self._lock:
            current = self._gates.get(gate.account_id)
            if current is not None and gate.control_epoch <= current.control_epoch:
                raise ExecutionInvariantError("execution_gate_epoch_must_increase")
            self._gates[gate.account_id] = gate

    async def acquire_lease(
        self,
        *,
        account_id: str,
        holder_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        if ttl <= timedelta(0):
            raise ExecutionInvariantError("worker_lease_ttl_must_be_positive")
        async with self._lock:
            current = self._leases.get(account_id)
            if current is not None and current.is_active(now) and current.holder_id != holder_id:
                raise ExecutionInvariantError("worker_lease_is_held_by_another_worker")
            fencing_token = self._last_fencing_tokens.get(account_id, 0) + 1
            lease = WorkerLease(
                account_id=account_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
                acquired_at=now,
                expires_at=now + ttl,
            )
            self._last_fencing_tokens[account_id] = fencing_token
            self._leases[account_id] = lease
            return lease

    async def reserve_intent(self, intent: ExecutionIntent, *, now: datetime) -> bool:
        async with self._lock:
            self._authorize_intent(intent, now)
            account = self._accounts.get(intent.account_id)
            if account is None:
                raise ExecutionInvariantError("paper_account_is_not_configured")
            existing_intent_id = self._intent_ids_by_semantic_key.get(intent.semantic_key)
            if existing_intent_id is not None:
                existing_intent = self._intents_by_id[existing_intent_id]
                if existing_intent_id == intent.id:
                    if existing_intent != intent:
                        raise ExecutionInvariantError(
                            "execution_intent_idempotency_payload_conflict"
                        )
                    return True
                return False
            if intent.id in self._intents_by_id:
                raise ExecutionInvariantError("execution_intent_id_already_exists")
            if intent.side == "sell" and any(
                reservation.account_id == intent.account_id
                and reservation.symbol == intent.symbol
                and reservation.side == "sell"
                and not reservation.terminal
                for reservation in self._reservations_by_intent.values()
            ):
                raise ExecutionInvariantError(
                    "paper_account_has_active_sell_reservation"
                )
            if intent.side == "buy":
                available_cash = account.cash_krw - account.reserved_cash_krw
                if available_cash < intent.cash_commitment_krw:
                    raise ExecutionInvariantError("paper_account_has_insufficient_available_cash")
                account.reserved_cash_krw += intent.cash_commitment_krw
                reservation = _IntentReservation(
                    account_id=intent.account_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    remaining_cash_krw=intent.cash_commitment_krw,
                    remaining_quantity=0,
                )
            else:
                position = account.positions.get(intent.symbol)
                position_quantity = position.quantity if position is not None else 0
                already_reserved = account.reserved_position_quantities.get(intent.symbol, 0)
                if position_quantity - already_reserved < intent.quantity:
                    raise ExecutionInvariantError(
                        "paper_account_has_insufficient_available_position"
                    )
                account.reserved_position_quantities[intent.symbol] = (
                    already_reserved + intent.quantity
                )
                reservation = _IntentReservation(
                    account_id=intent.account_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    remaining_cash_krw=0,
                    remaining_quantity=intent.quantity,
                )
            pinned_position_cost_basis = (
                PaperPositionCostBasis(
                    symbol=intent.symbol,
                    quantity=position.quantity,
                    total_cost_krw=position.total_cost_krw,
                )
                if intent.side == "sell" and position is not None
                else None
            )
            self._intents_by_id[intent.id] = intent
            self._intent_ids_by_semantic_key[intent.semantic_key] = intent.id
            self._reservations_by_intent[intent.id] = reservation
            self._position_cost_basis_by_intent[intent.id] = pinned_position_cost_basis
            self._observations_by_intent[intent.id] = ()
            return True

    async def execute_paper(
        self,
        intent: ExecutionIntent,
        bars: Sequence[MinuteBar],
        *,
        cost_schedule: ExecutionCostSchedule | None,
        execution_evidence: PaperExecutionEvidence | None,
        now: datetime,
        position_cost_basis: PaperPositionCostBasis | None = None,
    ) -> PaperSimulationResult:
        async with self._lock:
            if self.environment != "paper" or intent.environment != "paper":
                raise ExecutionInvariantError("paper_execution_requires_paper_kernel")
            self._authorize_intent(intent, now)
            if self._intents_by_id.get(intent.id) != intent:
                raise ExecutionInvariantError("execution_intent_is_not_reserved")
            if intent.id not in self._dispatch_started:
                raise ExecutionInvariantError("paper_simulation_requires_dispatch_marker")
            existing_result = self._results_by_intent.get(intent.id)
            if existing_result is not None and _paper_result_is_terminal(existing_result):
                return existing_result
            account = self._accounts[intent.account_id]
            pinned_position_cost_basis = self._position_cost_basis_by_intent[intent.id]
            if (
                position_cost_basis is not None
                and position_cost_basis != pinned_position_cost_basis
            ):
                raise ExecutionInvariantError(
                    "paper_position_cost_basis_snapshot_mismatch"
                )
            capacity_aware_bars = tuple(
                replace(
                    bar,
                    other_intent_filled_quantity=max(
                        bar.other_intent_filled_quantity,
                        self._other_intent_bar_usage(intent, bar),
                    ),
                )
                for bar in bars
            )
            result = self._paper_simulator.simulate(
                intent,
                capacity_aware_bars,
                cost_schedule=cost_schedule,
                execution_evidence=execution_evidence,
                now=now,
                position_cost_basis=pinned_position_cost_basis,
            )
            self._validate_result(intent, result)
            prior_fill_count = len(existing_result.fills) if existing_result is not None else 0
            prior_observation_count = (
                len(existing_result.observations) if existing_result is not None else 0
            )
            if existing_result is not None and (
                result.fills[:prior_fill_count] != existing_result.fills
                or result.observations[:prior_observation_count]
                != existing_result.observations
                or result.accounting_transactions[:prior_fill_count]
                != existing_result.accounting_transactions
            ):
                raise ExecutionInvariantError("paper_replay_prefix_mismatch")
            new_fills = result.fills[prior_fill_count:]
            new_transactions = result.accounting_transactions[prior_fill_count:]
            for transaction in new_transactions:
                if transaction.id in self._transactions_by_id:
                    raise ExecutionInvariantError("duplicate_accounting_transaction")
            self._settle_paper_result(
                account,
                intent,
                new_fills,
                terminal=_paper_result_is_terminal(result),
            )
            self._observations_by_intent[intent.id] = result.observations
            for transaction in new_transactions:
                self._transactions_by_id[transaction.id] = transaction
            self._results_by_intent[intent.id] = result
            return result

    def _other_intent_bar_usage(
        self,
        intent: ExecutionIntent,
        bar: MinuteBar,
    ) -> int:
        return sum(
            fill.quantity
            for other_intent_id, result in self._results_by_intent.items()
            if other_intent_id != intent.id
            and self._intents_by_id[other_intent_id].account_id == intent.account_id
            and self._intents_by_id[other_intent_id].symbol == intent.symbol
            for fill in result.fills
            if fill.filled_at == bar.completed_at
        )

    def _settle_paper_result(
        self,
        account: _MutablePaperAccount,
        intent: ExecutionIntent,
        fills: Sequence[PaperFill],
        *,
        terminal: bool,
    ) -> None:
        reservation = self._reservations_by_intent[intent.id]
        if reservation.terminal:
            raise ExecutionInvariantError("execution_reservation_is_already_terminal")
        new_cash = account.cash_krw
        new_reserved_cash = account.reserved_cash_krw
        new_positions = {
            symbol: _MutablePosition(item.quantity, item.total_cost_krw)
            for symbol, item in account.positions.items()
        }
        new_reserved_positions = dict(account.reserved_position_quantities)
        remaining_cash = reservation.remaining_cash_krw
        remaining_quantity = reservation.remaining_quantity
        for fill in fills:
            gross = fill.gross_amount_krw
            if intent.side == "buy":
                settled_cash = gross + fill.commission_krw
                if settled_cash > remaining_cash:
                    raise ExecutionInvariantError("paper_fill_exceeds_reserved_cash")
                new_cash -= settled_cash
                new_reserved_cash -= settled_cash
                remaining_cash -= settled_cash
                if new_cash < 0 or new_reserved_cash < 0:
                    raise ExecutionInvariantError("paper_account_cash_invariant_failed")
                position = new_positions.get(intent.symbol)
                if position is None:
                    new_positions[intent.symbol] = _MutablePosition(
                        quantity=fill.quantity,
                        total_cost_krw=gross,
                    )
                else:
                    position.quantity += fill.quantity
                    position.total_cost_krw += gross
            else:
                position = new_positions.get(intent.symbol)
                if position is None or position.quantity < fill.quantity:
                    raise ExecutionInvariantError("paper_account_position_invariant_failed")
                if position.total_cost_krw < fill.position_cost_relief_krw:
                    raise ExecutionInvariantError("paper_position_cost_relief_exceeds_basis")
                position.quantity -= fill.quantity
                position.total_cost_krw -= fill.position_cost_relief_krw
                remaining_quantity -= fill.quantity
                new_reserved_positions[intent.symbol] -= fill.quantity
                if remaining_quantity < 0 or new_reserved_positions[intent.symbol] < 0:
                    raise ExecutionInvariantError("paper_position_reservation_invariant_failed")
                if position.quantity == 0:
                    if position.total_cost_krw != 0:
                        raise ExecutionInvariantError("paper_closed_position_retains_cost")
                    del new_positions[intent.symbol]
                new_cash += gross - fill.commission_krw - fill.tax_krw
        if terminal:
            if intent.side == "buy":
                new_reserved_cash -= remaining_cash
                remaining_cash = 0
            else:
                new_reserved_positions[intent.symbol] -= remaining_quantity
                remaining_quantity = 0
                if new_reserved_positions[intent.symbol] == 0:
                    del new_reserved_positions[intent.symbol]
        if new_reserved_cash < 0:
            raise ExecutionInvariantError("paper_cash_reservation_release_failed")
        account.cash_krw = new_cash
        account.reserved_cash_krw = new_reserved_cash
        account.positions = new_positions
        account.reserved_position_quantities = new_reserved_positions
        reservation.remaining_cash_krw = remaining_cash
        reservation.remaining_quantity = remaining_quantity
        reservation.terminal = terminal

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> None:
        async with self._lock:
            if intent.environment != self.environment:
                raise ExecutionInvariantError("dispatch_environment_mismatch")
            self._authorize_intent(intent, now)
            if now < intent.eligible_at or now > intent.expires_at:
                raise ExecutionInvariantError("dispatch_outside_execution_window")
            if self._intents_by_id.get(intent.id) != intent:
                raise ExecutionInvariantError("execution_intent_is_not_reserved")
            if intent.id in self._dispatch_started:
                return
            if self._observations_by_intent[intent.id]:
                raise ExecutionInvariantError("execution_dispatch_has_existing_observation")
            self._dispatch_started.add(intent.id)

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        now: datetime,
    ) -> None:
        async with self._lock:
            if self.environment != "contract_test" or intent.environment != "contract_test":
                raise ExecutionInvariantError("observation_requires_contract_test_kernel")
            self._authorize_intent(intent, now)
            if self._intents_by_id.get(intent.id) != intent:
                raise ExecutionInvariantError("execution_intent_is_not_reserved")
            if (
                intent.id not in self._dispatch_started
                and observation.status != "failed_pre_dispatch"
            ):
                raise ExecutionInvariantError("execution_observation_precedes_dispatch_marker")
            current = self._observations_by_intent[intent.id]
            if observation.sequence <= len(current):
                existing_observation = current[observation.sequence - 1]
                existing_transaction = next(
                    (
                        transaction
                        for transaction in self._transactions_by_id.values()
                        if transaction.intent_id == intent.id
                        and transaction.observation_sequence == observation.sequence
                    ),
                    None,
                )
                if (
                    existing_observation != observation
                    or accounting_transaction != existing_transaction
                    or (existing_observation.last_fill_quantity is not None)
                    != (existing_transaction is not None)
                ):
                    raise ExecutionInvariantError(
                        "contract_observation_replay_conflict"
                    )
                return
            if current and (
                current[-1].status in TERMINAL_EXECUTION_STATUSES
                or current[-1].status in BLOCKING_EXECUTION_STATUSES
            ):
                raise ExecutionInvariantError("execution_observation_follows_blocking_state")
            if current and observation.provider_order_id != current[-1].provider_order_id:
                raise ExecutionInvariantError("provider_order_identity_mismatch")
            if observation.intent_id != intent.id or observation.sequence != len(current) + 1:
                raise ExecutionInvariantError("execution_observation_sequence_is_invalid")
            if observation.cumulative_quantity > intent.quantity:
                raise ExecutionInvariantError("execution_observation_exceeds_intent_quantity")
            if current and observation.cumulative_quantity < current[-1].cumulative_quantity:
                raise ExecutionInvariantError("execution_observation_cumulative_values_regressed")
            if current and (
                observation.cumulative_gross_krw < current[-1].cumulative_gross_krw
                or observation.cumulative_commission_krw
                < current[-1].cumulative_commission_krw
                or observation.cumulative_tax_krw < current[-1].cumulative_tax_krw
            ):
                raise ExecutionInvariantError("execution_observation_cumulative_values_regressed")
            if (
                observation.status == "filled"
                and observation.cumulative_quantity != intent.quantity
            ):
                raise ExecutionInvariantError("filled_observation_does_not_complete_quantity")
            if observation.status == "failed_pre_dispatch" and any(
                (
                    observation.cumulative_quantity,
                    observation.cumulative_gross_krw,
                    observation.cumulative_commission_krw,
                    observation.cumulative_tax_krw,
                )
            ):
                raise ExecutionInvariantError("failed_pre_dispatch_observation_has_execution")
            has_fill = observation.last_fill_quantity is not None
            if has_fill != (accounting_transaction is not None):
                raise ExecutionInvariantError(
                    "contract_fill_observation_requires_accounting_transaction"
                )
            if accounting_transaction is not None:
                if (
                    accounting_transaction.intent_id != intent.id
                    or accounting_transaction.observation_sequence
                    != observation.sequence
                    or accounting_transaction.posted_at != observation.observed_at
                ):
                    raise ExecutionInvariantError(
                        "contract_accounting_transaction_identity_mismatch"
                    )
                if accounting_transaction.id in self._transactions_by_id:
                    raise ExecutionInvariantError("duplicate_accounting_transaction")
            if observation.provider_execution_id is not None:
                provider_binding = (
                    intent.id,
                    observation.sequence,
                    observation.provider_observation_sha256,
                )
                existing_provider_binding = self._provider_execution_bindings.get(
                    observation.provider_execution_id
                )
                if (
                    existing_provider_binding is not None
                    and existing_provider_binding != provider_binding
                ):
                    raise ExecutionInvariantError(
                        "provider_execution_identity_reused"
                    )
            expected_transaction = self._expected_contract_accounting_transaction(
                intent,
                current,
                observation,
            )
            if accounting_transaction != expected_transaction:
                raise ExecutionInvariantError(
                    "contract_accounting_transaction_projection_mismatch"
                )
            self._apply_contract_observation(intent, current, observation)
            self._observations_by_intent[intent.id] = (*current, observation)
            if accounting_transaction is not None:
                self._transactions_by_id[accounting_transaction.id] = accounting_transaction
            if observation.provider_execution_id is not None:
                self._provider_execution_bindings[
                    observation.provider_execution_id
                ] = (
                    intent.id,
                    observation.sequence,
                    observation.provider_observation_sha256,
                )

    def _expected_contract_accounting_transaction(
        self,
        intent: ExecutionIntent,
        current: tuple[ExecutionObservation, ...],
        observation: ExecutionObservation,
    ) -> AccountingTransaction | None:
        previous = current[-1] if current else None
        previous_quantity = previous.cumulative_quantity if previous else 0
        previous_gross = previous.cumulative_gross_krw if previous else 0
        previous_commission = previous.cumulative_commission_krw if previous else 0
        previous_tax = previous.cumulative_tax_krw if previous else 0
        fill_quantity = observation.cumulative_quantity - previous_quantity
        gross_delta = observation.cumulative_gross_krw - previous_gross
        commission_delta = observation.cumulative_commission_krw - previous_commission
        tax_delta = observation.cumulative_tax_krw - previous_tax
        if fill_quantity == 0:
            if (
                observation.last_fill_quantity is not None
                or gross_delta != 0
                or commission_delta != 0
                or tax_delta != 0
            ):
                raise ExecutionInvariantError("contract_observation_fill_delta_is_invalid")
            return None
        if (
            observation.last_fill_quantity != fill_quantity
            or observation.last_fill_price_krw is None
            or observation.last_fill_settlement_date is None
            or gross_delta != fill_quantity * observation.last_fill_price_krw
        ):
            raise ExecutionInvariantError("contract_observation_fill_delta_is_invalid")

        position_cost_relief = 0
        if intent.side == "sell":
            position = self._accounts[intent.account_id].positions.get(intent.symbol)
            if position is None or position.quantity < fill_quantity:
                raise ExecutionInvariantError("contract_position_invariant_failed")
            position_cost_relief = (
                position.total_cost_krw
                if fill_quantity == position.quantity
                else position.total_cost_krw * fill_quantity // position.quantity
            )
        fill_sequence = 1 + sum(
            item.last_fill_quantity is not None for item in current
        )
        fill = PaperFill(
            sequence=fill_sequence,
            filled_at=observation.observed_at,
            quantity=fill_quantity,
            price_krw=observation.last_fill_price_krw,
            commission_krw=commission_delta,
            tax_krw=tax_delta,
            settlement_date=observation.last_fill_settlement_date,
            position_cost_relief_krw=position_cost_relief,
            realized_pnl_krw=(
                gross_delta - position_cost_relief - commission_delta - tax_delta
                if intent.side == "sell"
                else 0
            ),
        )
        return build_fill_accounting_transaction(intent, fill, observation.sequence)

    def _apply_contract_observation(
        self,
        intent: ExecutionIntent,
        current: tuple[ExecutionObservation, ...],
        observation: ExecutionObservation,
    ) -> None:
        previous = current[-1] if current else None
        previous_quantity = previous.cumulative_quantity if previous else 0
        previous_gross = previous.cumulative_gross_krw if previous else 0
        previous_commission = previous.cumulative_commission_krw if previous else 0
        previous_tax = previous.cumulative_tax_krw if previous else 0
        fill_quantity = observation.cumulative_quantity - previous_quantity
        gross_delta = observation.cumulative_gross_krw - previous_gross
        commission_delta = observation.cumulative_commission_krw - previous_commission
        tax_delta = observation.cumulative_tax_krw - previous_tax
        if fill_quantity == 0:
            if observation.last_fill_quantity is not None or gross_delta != 0:
                raise ExecutionInvariantError("contract_observation_fill_delta_is_invalid")
        else:
            if (
                observation.last_fill_quantity != fill_quantity
                or observation.last_fill_price_krw is None
                or gross_delta != fill_quantity * observation.last_fill_price_krw
            ):
                raise ExecutionInvariantError("contract_observation_fill_delta_is_invalid")

        reservation = self._reservations_by_intent[intent.id]
        if reservation.terminal:
            raise ExecutionInvariantError("execution_reservation_is_already_terminal")
        account = self._accounts[intent.account_id]
        new_cash = account.cash_krw
        new_reserved_cash = account.reserved_cash_krw
        new_positions = {
            symbol: _MutablePosition(item.quantity, item.total_cost_krw)
            for symbol, item in account.positions.items()
        }
        new_reserved_positions = dict(account.reserved_position_quantities)
        remaining_cash = reservation.remaining_cash_krw
        remaining_quantity = reservation.remaining_quantity
        if fill_quantity > 0 and intent.side == "buy":
            if tax_delta != 0:
                raise ExecutionInvariantError("paper_buy_fill_cannot_have_tax")
            settled_cash = gross_delta + commission_delta
            if settled_cash > remaining_cash or settled_cash > new_cash:
                raise ExecutionInvariantError("contract_fill_exceeds_reserved_cash")
            new_cash -= settled_cash
            new_reserved_cash -= settled_cash
            remaining_cash -= settled_cash
            position = new_positions.get(intent.symbol)
            if position is None:
                new_positions[intent.symbol] = _MutablePosition(
                    quantity=fill_quantity,
                    total_cost_krw=gross_delta,
                )
            else:
                position.quantity += fill_quantity
                position.total_cost_krw += gross_delta
        elif fill_quantity > 0:
            position = new_positions.get(intent.symbol)
            if position is None or position.quantity < fill_quantity:
                raise ExecutionInvariantError("contract_position_invariant_failed")
            if remaining_quantity < fill_quantity:
                raise ExecutionInvariantError("contract_fill_exceeds_reserved_position")
            cost_relief = (
                position.total_cost_krw
                if fill_quantity == position.quantity
                else position.total_cost_krw * fill_quantity // position.quantity
            )
            position.quantity -= fill_quantity
            position.total_cost_krw -= cost_relief
            remaining_quantity -= fill_quantity
            new_reserved_positions[intent.symbol] -= fill_quantity
            if position.quantity == 0:
                if position.total_cost_krw != 0:
                    raise ExecutionInvariantError("paper_closed_position_retains_cost")
                del new_positions[intent.symbol]
            net_proceeds = gross_delta - commission_delta - tax_delta
            if net_proceeds < 0:
                raise ExecutionInvariantError("contract_fill_has_negative_net_proceeds")
            new_cash += net_proceeds

        if observation.status in TERMINAL_EXECUTION_STATUSES:
            if intent.side == "buy":
                new_reserved_cash -= remaining_cash
                remaining_cash = 0
            else:
                new_reserved_positions[intent.symbol] -= remaining_quantity
                remaining_quantity = 0
                if new_reserved_positions[intent.symbol] == 0:
                    del new_reserved_positions[intent.symbol]
        if new_cash < 0 or new_reserved_cash < 0:
            raise ExecutionInvariantError("contract_cash_projection_invariant_failed")
        if any(quantity < 0 for quantity in new_reserved_positions.values()):
            raise ExecutionInvariantError("contract_position_reservation_invariant_failed")
        account.cash_krw = new_cash
        account.reserved_cash_krw = new_reserved_cash
        account.positions = new_positions
        account.reserved_position_quantities = new_reserved_positions
        reservation.remaining_cash_krw = remaining_cash
        reservation.remaining_quantity = remaining_quantity
        reservation.terminal = observation.status in TERMINAL_EXECUTION_STATUSES

    def _release_reservation(self, intent_id: str) -> None:
        reservation = self._reservations_by_intent[intent_id]
        if reservation.terminal:
            return
        account = self._accounts[reservation.account_id]
        if reservation.side == "buy":
            account.reserved_cash_krw -= reservation.remaining_cash_krw
            reservation.remaining_cash_krw = 0
        else:
            account.reserved_position_quantities[reservation.symbol] -= (
                reservation.remaining_quantity
            )
            if account.reserved_position_quantities[reservation.symbol] == 0:
                del account.reserved_position_quantities[reservation.symbol]
            reservation.remaining_quantity = 0
        reservation.terminal = True

    async def account_snapshot(self, account_id: str) -> PaperAccountSnapshot:
        async with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                raise ExecutionInvariantError("paper_account_is_not_configured")
            return PaperAccountSnapshot(
                account_id=account_id,
                cash_krw=account.cash_krw,
                reserved_cash_krw=account.reserved_cash_krw,
                positions=tuple(
                    PaperPositionSnapshot(
                        symbol=symbol,
                        quantity=position.quantity,
                        total_cost_krw=position.total_cost_krw,
                    )
                    for symbol, position in sorted(account.positions.items())
                ),
                reserved_position_quantities=tuple(
                    sorted(account.reserved_position_quantities.items())
                ),
            )

    async def observations_for(self, intent_id: str) -> tuple[ExecutionObservation, ...]:
        async with self._lock:
            return self._observations_by_intent.get(intent_id, ())

    async def accounting_transactions_for(
        self,
        intent_id: str,
    ) -> tuple[AccountingTransaction, ...]:
        async with self._lock:
            return tuple(
                transaction
                for transaction in self._transactions_by_id.values()
                if transaction.intent_id == intent_id
            )

    async def intent_for_semantic_key(self, semantic_key: str) -> ExecutionIntent | None:
        async with self._lock:
            intent_id = self._intent_ids_by_semantic_key.get(semantic_key)
            return self._intents_by_id.get(intent_id) if intent_id is not None else None

    def _authorize_intent(self, intent: ExecutionIntent, now: datetime) -> None:
        if intent.environment != self.environment:
            raise ExecutionInvariantError("execution_intent_environment_mismatch")
        gate = self._gates.get(intent.account_id)
        if gate is None or not gate.authorizes(intent.environment, now):
            raise ExecutionInvariantError("execution_gate_is_closed")
        if gate.control_epoch != intent.gate_epoch:
            raise ExecutionInvariantError("execution_intent_has_stale_gate_epoch")
        lease = self._leases.get(intent.account_id)
        if lease is None or not lease.is_active(now):
            raise ExecutionInvariantError("worker_lease_is_not_active")
        if (
            lease.holder_id != intent.lease_holder_id
            or lease.fencing_token != intent.lease_fencing_token
        ):
            raise ExecutionInvariantError("execution_intent_has_stale_fencing_token")

    @staticmethod
    def _validate_result(intent: ExecutionIntent, result: PaperSimulationResult) -> None:
        previous_quantity = 0
        for expected_sequence, observation in enumerate(result.observations, start=1):
            if observation.intent_id != intent.id or observation.sequence != expected_sequence:
                raise ExecutionInvariantError("paper_observation_sequence_is_invalid")
            if observation.cumulative_quantity < previous_quantity:
                raise ExecutionInvariantError("paper_observation_cumulative_values_regressed")
            if observation.cumulative_quantity > intent.quantity:
                raise ExecutionInvariantError("paper_observation_exceeds_intent_quantity")
            previous_quantity = observation.cumulative_quantity
        if not result.observations:
            return
        final_observation = result.observations[-1]
        if final_observation.status == "filled":
            if final_observation.cumulative_quantity != intent.quantity:
                raise ExecutionInvariantError("filled_result_does_not_complete_quantity")
        elif final_observation.status == "expired":
            if final_observation.cumulative_quantity >= intent.quantity:
                raise ExecutionInvariantError("expired_result_has_complete_quantity")
        elif final_observation.status == "partial_filled":
            if final_observation.cumulative_quantity >= intent.quantity:
                raise ExecutionInvariantError("partial_result_completes_quantity")
        else:
            raise ExecutionInvariantError("paper_result_status_is_invalid")
        for fill, transaction in zip(result.fills, result.accounting_transactions, strict=True):
            if transaction.intent_id != intent.id:
                raise ExecutionInvariantError("accounting_transaction_intent_mismatch")
            if transaction.observation_sequence != fill.sequence:
                raise ExecutionInvariantError("accounting_transaction_sequence_mismatch")


def _paper_result_is_terminal(result: PaperSimulationResult) -> bool:
    return bool(
        result.observations
        and result.observations[-1].status in TERMINAL_EXECUTION_STATUSES
    )
