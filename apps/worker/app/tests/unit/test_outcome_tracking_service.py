from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.application.ports.outcome_tracking_port import OutcomeTrackingRows
from app.application.services.outcome_tracking_service import OutcomeTrackingService
from app.domain.common.json import JsonObject

NOW = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)


async def test_calculates_paper_outcome_from_fixed_price_fixture() -> None:
    repository = FakeOutcomeRepository(
        rows=OutcomeTrackingRows(
            decisions=[
                _decision(
                    "decision-1",
                    action="buy",
                    feature_snapshot={
                        "price_at_decision": 100,
                        "target_sell_price": 105,
                        "stop_loss_price": 95,
                    },
                )
            ],
            orders=[_order("order-1", "decision-1", quantity=10, price=100)],
            features_daily=_future_prices([102, 103, 94, 104, 105, *([110] * 14), 120]),
            existing_outcomes=[],
        )
    )

    summary = await OutcomeTrackingService(repository).update_once(NOW)

    outcome = repository.outcomes_by_decision["decision-1"]
    assert summary.upserted_count == 1
    assert outcome["return_1d"] == 0.02
    assert outcome["return_5d"] == 0.05
    assert outcome["return_20d"] == 0.2
    assert outcome["max_drawdown_20d"] == -0.06
    assert outcome["hit_target"] is True
    assert outcome["hit_stop"] is True
    assert outcome["realized_pnl_krw"] == 200
    assert outcome["outcome_status"] == "complete"


async def test_update_once_is_idempotent_and_updates_when_future_prices_arrive() -> None:
    repository = FakeOutcomeRepository(
        rows=OutcomeTrackingRows(
            decisions=[_decision("decision-1", action="buy")],
            orders=[],
            features_daily=_future_prices([101, 102, 103, 104, 105]),
            existing_outcomes=[],
        )
    )
    service = OutcomeTrackingService(repository)

    await service.update_once(NOW)
    repository.rows = OutcomeTrackingRows(
        decisions=[_decision("decision-1", action="buy")],
        orders=[],
        features_daily=_future_prices([101, 102, 103, 104, 105, *([106] * 14), 110]),
        existing_outcomes=list(repository.outcomes_by_decision.values()),
    )
    await service.update_once(NOW + timedelta(minutes=1))

    assert len(repository.outcomes_by_decision) == 1
    outcome = repository.outcomes_by_decision["decision-1"]
    assert outcome["return_5d"] == 0.05
    assert outcome["return_20d"] == 0.1
    assert outcome["outcome_status"] == "complete"


async def test_skips_decision_without_price_at_decision() -> None:
    repository = FakeOutcomeRepository(
        rows=OutcomeTrackingRows(
            decisions=[_decision("decision-1", action="buy", feature_snapshot={})],
            orders=[],
            features_daily=_future_prices([101, 102, 103]),
            existing_outcomes=[],
        )
    )

    summary = await OutcomeTrackingService(repository).update_once(NOW)

    outcome = repository.outcomes_by_decision["decision-1"]
    assert summary.skipped_count == 1
    assert outcome["outcome_status"] == "skipped"
    assert outcome["reason"] == "missing_price_at_decision"


async def test_hold_and_blocked_actions_are_skipped() -> None:
    repository = FakeOutcomeRepository(
        rows=OutcomeTrackingRows(
            decisions=[
                _decision("decision-hold", action="hold"),
                _decision("decision-blocked", action="blocked"),
            ],
            orders=[],
            features_daily=_future_prices([101, 102, 103]),
            existing_outcomes=[],
        )
    )

    summary = await OutcomeTrackingService(repository).update_once(NOW)

    assert summary.skipped_count == 2
    assert repository.outcomes_by_decision["decision-hold"]["reason"] == "non_trade_action"
    assert repository.outcomes_by_decision["decision-blocked"]["reason"] == "non_trade_action"


async def test_marks_pending_when_future_price_data_is_missing() -> None:
    repository = FakeOutcomeRepository(
        rows=OutcomeTrackingRows(
            decisions=[_decision("decision-1", action="buy")],
            orders=[],
            features_daily=[],
            existing_outcomes=[],
        )
    )

    summary = await OutcomeTrackingService(repository).update_once(NOW)

    outcome = repository.outcomes_by_decision["decision-1"]
    assert summary.upserted_count == 1
    assert summary.complete_count == 0
    assert outcome["outcome_status"] == "pending"
    assert outcome["reason"] == "missing_future_price_data"


@dataclass(slots=True)
class FakeOutcomeRepository:
    rows: OutcomeTrackingRows
    outcomes_by_decision: dict[str, JsonObject] = field(default_factory=dict)

    async def load_outcome_tracking_rows(
        self, decision_limit: int, price_limit: int
    ) -> OutcomeTrackingRows:
        return self.rows

    async def upsert_outcome(self, outcome: JsonObject) -> None:
        decision_id = outcome.get("decision_id")
        if isinstance(decision_id, str):
            self.outcomes_by_decision[decision_id] = outcome


def _decision(
    decision_id: str,
    *,
    action: str,
    feature_snapshot: JsonObject | None = None,
) -> JsonObject:
    return {
        "id": decision_id,
        "symbol": "005930",
        "action": action,
        "final_score": 0.72,
        "decided_at": "2026-01-02T00:00:00+00:00",
        "created_at": "2026-01-02T00:00:00+00:00",
        "feature_snapshot": (
            feature_snapshot if feature_snapshot is not None else {"price_at_decision": 100}
        ),
    }


def _order(order_id: str, decision_id: str, *, quantity: int, price: int) -> JsonObject:
    return {
        "id": order_id,
        "decision_id": decision_id,
        "symbol": "005930",
        "side": "buy",
        "status": "paper",
        "quantity": quantity,
        "price": price,
        "amount_krw": quantity * price,
        "created_at": "2026-01-02T00:00:00+00:00",
    }


def _future_prices(prices: list[int]) -> list[JsonObject]:
    trade_date = date(2026, 1, 3)
    rows: list[JsonObject] = []
    for price in prices:
        while trade_date.weekday() >= 5:
            trade_date += timedelta(days=1)
        rows.append(
            {
                "symbol": "005930",
                "trade_date": trade_date.isoformat(),
                "close_price": price,
                "raw_snapshot": {"close_price": price},
            }
        )
        trade_date += timedelta(days=1)
    return rows
