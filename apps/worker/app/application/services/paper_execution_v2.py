from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    ExecutionStatus,
    LedgerPosting,
    MinuteBar,
    PaperExecutionEvidence,
    PaperFill,
    PaperPositionCostBasis,
    PaperSimulationResult,
    new_accounting_transaction_id,
)

PAPER_PARTICIPATION_RATE = Decimal("0.01")
PAPER_ADVERSE_SLIPPAGE_RATE = Decimal("0.001")


class DeterministicPaperExecutionSimulator:
    """Deterministic LIMIT/DAY simulator without same-minute look-ahead."""

    def simulate(
        self,
        intent: ExecutionIntent,
        bars: Sequence[MinuteBar],
        *,
        cost_schedule: ExecutionCostSchedule | None,
        execution_evidence: PaperExecutionEvidence | None,
        now: datetime,
        position_cost_basis: PaperPositionCostBasis | None = None,
    ) -> PaperSimulationResult:
        if intent.environment != "paper":
            raise ExecutionInvariantError("paper_simulator_requires_paper_environment")
        if cost_schedule is None:
            raise ExecutionInvariantError("paper_execution_cost_schedule_is_required")
        if not cost_schedule.covers(intent.eligible_at, intent.expires_at):
            raise ExecutionInvariantError("paper_execution_cost_schedule_is_not_effective")
        if (
            cost_schedule.version != intent.cost_schedule_version
            or cost_schedule.evidence_sha256 != intent.cost_schedule_evidence_sha256
        ):
            raise ExecutionInvariantError("paper_execution_cost_schedule_identity_mismatch")
        if execution_evidence is None:
            raise ExecutionInvariantError("paper_execution_evidence_is_required")
        if not execution_evidence.covers(intent.eligible_at, intent.expires_at):
            raise ExecutionInvariantError("paper_execution_evidence_is_not_effective")
        if execution_evidence.execution_policy_version != intent.execution_policy_version:
            raise ExecutionInvariantError("paper_execution_policy_evidence_mismatch")
        if not execution_evidence.validates_price(intent.limit_price_krw):
            raise ExecutionInvariantError("paper_limit_price_violates_tick_rule")
        if intent.side == "sell" and (
            position_cost_basis is None
            or position_cost_basis.symbol != intent.symbol
            or position_cost_basis.quantity < intent.quantity
        ):
            raise ExecutionInvariantError("paper_sell_position_cost_basis_is_required")
        eligible_bars = self._eligible_bars(intent, bars, now=now)
        for bar in eligible_bars:
            if bar.source_sha256 != execution_evidence.volume_evidence_sha256:
                raise ExecutionInvariantError("paper_bar_source_evidence_mismatch")
            if not all(
                execution_evidence.validates_price(price)
                for price in (bar.open_krw, bar.high_krw, bar.low_krw, bar.close_krw)
            ):
                raise ExecutionInvariantError("paper_bar_price_violates_tick_rule")
        remaining = intent.quantity
        remaining_cost_quantity = position_cost_basis.quantity if position_cost_basis else 0
        remaining_position_cost = position_cost_basis.total_cost_krw if position_cost_basis else 0
        cumulative_quantity = 0
        cumulative_gross = 0
        cumulative_commission = 0
        cumulative_tax = 0
        fills: list[PaperFill] = []
        observations: list[ExecutionObservation] = []
        transactions: list[AccountingTransaction] = []

        for bar in eligible_bars:
            participation_capacity = _floor_decimal(
                Decimal(bar.volume) * PAPER_PARTICIPATION_RATE
            )
            if bar.other_intent_filled_quantity > participation_capacity:
                raise ExecutionInvariantError(
                    "paper_bar_participation_usage_exceeds_capacity"
                )
            available_quantity = (
                participation_capacity - bar.other_intent_filled_quantity
            )
            if available_quantity <= 0:
                continue
            fill_price = _limit_fill_price(
                intent,
                bar,
                tick_size_krw=execution_evidence.tick_size_krw,
            )
            if fill_price is None:
                continue
            if not execution_evidence.validates_price(fill_price):
                raise ExecutionInvariantError("paper_fill_price_violates_tick_rule")
            fill_quantity = min(remaining, available_quantity)
            gross_amount = fill_quantity * fill_price
            commission_rate = (
                cost_schedule.buy_commission_rate
                if intent.side == "buy"
                else cost_schedule.sell_commission_rate
            )
            commission = _ceil_decimal(Decimal(gross_amount) * commission_rate)
            tax = (
                _ceil_decimal(Decimal(gross_amount) * cost_schedule.sell_tax_rate)
                if intent.side == "sell"
                else 0
            )
            position_cost_relief = 0
            realized_pnl = 0
            if intent.side == "sell":
                position_cost_relief = (
                    remaining_position_cost
                    if fill_quantity == remaining_cost_quantity
                    else remaining_position_cost * fill_quantity // remaining_cost_quantity
                )
                remaining_cost_quantity -= fill_quantity
                remaining_position_cost -= position_cost_relief
                realized_pnl = gross_amount - position_cost_relief - commission - tax
            sequence = len(observations) + 1
            settlement_date = execution_evidence.settlement_date_for(
                bar.completed_at,
                cost_schedule.settlement_days,
            )
            fill = PaperFill(
                sequence=sequence,
                filled_at=bar.completed_at,
                quantity=fill_quantity,
                price_krw=fill_price,
                commission_krw=commission,
                tax_krw=tax,
                settlement_date=settlement_date,
                position_cost_relief_krw=position_cost_relief,
                realized_pnl_krw=realized_pnl,
            )
            fills.append(fill)
            remaining -= fill_quantity
            cumulative_quantity += fill_quantity
            cumulative_gross += gross_amount
            cumulative_commission += commission
            cumulative_tax += tax
            status: ExecutionStatus = "filled" if remaining == 0 else "partial_filled"
            observation = ExecutionObservation.create(
                intent_id=intent.id,
                sequence=sequence,
                status=status,
                observed_at=bar.completed_at,
                provider_order_id=f"paper:{intent.id}",
                provider_execution_id=f"paper:{intent.id}:fill:{sequence}",
                cumulative_quantity=cumulative_quantity,
                cumulative_gross_krw=cumulative_gross,
                cumulative_commission_krw=cumulative_commission,
                cumulative_tax_krw=cumulative_tax,
                last_fill_quantity=fill_quantity,
                last_fill_price_krw=fill_price,
                last_fill_settlement_date=settlement_date,
            )
            observations.append(observation)
            transactions.append(
                build_fill_accounting_transaction(
                    intent,
                    fill,
                    observation.sequence,
                )
            )
            if remaining == 0:
                break

        if remaining > 0 and now >= intent.expires_at:
            observations.append(
                ExecutionObservation.create(
                    intent_id=intent.id,
                    sequence=len(observations) + 1,
                    status="expired",
                    observed_at=max(
                        (intent.expires_at, *(bar.completed_at for bar in eligible_bars))
                    ),
                    provider_order_id=f"paper:{intent.id}",
                    provider_execution_id=None,
                    cumulative_quantity=cumulative_quantity,
                    cumulative_gross_krw=cumulative_gross,
                    cumulative_commission_krw=cumulative_commission,
                    cumulative_tax_krw=cumulative_tax,
                    reason="day_limit_order_expired",
                )
            )

        return PaperSimulationResult(
            fills=tuple(fills),
            observations=tuple(observations),
            accounting_transactions=tuple(transactions),
        )

    @staticmethod
    def _eligible_bars(
        intent: ExecutionIntent,
        bars: Sequence[MinuteBar],
        *,
        now: datetime,
    ) -> tuple[MinuteBar, ...]:
        matching = [bar for bar in bars if bar.symbol == intent.symbol]
        ordered = sorted(matching, key=lambda bar: bar.minute)
        if any(
            left.minute == right.minute
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            raise ExecutionInvariantError("duplicate_minute_bar")
        eligible = tuple(
            bar
            for bar in ordered
            if intent.eligible_at <= bar.minute <= intent.expires_at
            and bar.completed_at <= intent.expires_at
        )
        if any(
            not bar.is_complete or now < bar.completed_at or now < bar.as_of
            for bar in eligible
        ):
            raise ExecutionInvariantError("paper_bar_is_incomplete_or_from_future")
        return eligible


def _limit_fill_price(
    intent: ExecutionIntent,
    bar: MinuteBar,
    *,
    tick_size_krw: int,
) -> int | None:
    if intent.side == "buy":
        if bar.open_krw <= intent.limit_price_krw:
            reference_price = bar.open_krw
        elif bar.low_krw <= intent.limit_price_krw:
            reference_price = intent.limit_price_krw
        else:
            return None
        adverse_price = _ceil_to_tick(
            Decimal(reference_price) * (Decimal(1) + PAPER_ADVERSE_SLIPPAGE_RATE),
            tick_size_krw,
        )
        return min(intent.limit_price_krw, adverse_price)

    if bar.open_krw >= intent.limit_price_krw:
        reference_price = bar.open_krw
    elif bar.high_krw >= intent.limit_price_krw:
        reference_price = intent.limit_price_krw
    else:
        return None
    adverse_price = _floor_to_tick(
        Decimal(reference_price) * (Decimal(1) - PAPER_ADVERSE_SLIPPAGE_RATE),
        tick_size_krw,
    )
    return max(intent.limit_price_krw, adverse_price)


def build_fill_accounting_transaction(
    intent: ExecutionIntent,
    fill: PaperFill,
    observation_sequence: int,
) -> AccountingTransaction:
    gross = fill.gross_amount_krw
    postings: list[LedgerPosting]
    if intent.side == "buy":
        postings = [LedgerPosting("POSITION_COST", debit_krw=gross)]
        if fill.commission_krw:
            postings.append(LedgerPosting("FEES", debit_krw=fill.commission_krw))
        postings.append(
            LedgerPosting(
                "CASH",
                credit_krw=gross + fill.commission_krw,
            )
        )
    else:
        net_proceeds = gross - fill.commission_krw - fill.tax_krw
        postings = [LedgerPosting("CASH", debit_krw=net_proceeds)]
        if fill.commission_krw:
            postings.append(LedgerPosting("FEES", debit_krw=fill.commission_krw))
        if fill.tax_krw:
            postings.append(LedgerPosting("TAXES", debit_krw=fill.tax_krw))
        trading_pnl = gross - fill.position_cost_relief_krw
        if trading_pnl < 0:
            postings.append(LedgerPosting("REALIZED_PNL", debit_krw=-trading_pnl))
        postings.append(
            LedgerPosting(
                "POSITION_COST",
                credit_krw=fill.position_cost_relief_krw,
            )
        )
        if trading_pnl > 0:
            postings.append(LedgerPosting("REALIZED_PNL", credit_krw=trading_pnl))
    return AccountingTransaction(
        id=new_accounting_transaction_id(intent.id, fill.sequence),
        intent_id=intent.id,
        observation_sequence=observation_sequence,
        posted_at=fill.filled_at,
        postings=tuple(postings),
    )


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _floor_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil_to_tick(value: Decimal, tick_size_krw: int) -> int:
    return _ceil_decimal(value / Decimal(tick_size_krw)) * tick_size_krw


def _floor_to_tick(value: Decimal, tick_size_krw: int) -> int:
    return _floor_decimal(value / Decimal(tick_size_krw)) * tick_size_krw
