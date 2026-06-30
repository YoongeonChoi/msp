from datetime import UTC, datetime
from uuid import uuid4

from app.adapters.persistence.models import decision_to_row
from app.domain.trading.entities import DecisionSnapshot, Signal


def test_decision_to_row_uses_base_created_at_schema() -> None:
    created_at = datetime(2026, 6, 30, 9, 0, tzinfo=UTC)
    snapshot = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=Signal(
            symbol="005930",
            action="hold",
            final_score=0.55,
            confidence=0.7,
            order_amount_krw=100_000,
            sector="technology",
        ),
        strategy_version_id=uuid4(),
        created_at=created_at,
        feature_snapshot={"raw": {}},
        risk_snapshot={},
    )

    row = decision_to_row(snapshot)

    assert row["created_at"] == created_at.isoformat()
    assert "decided_at" not in row
