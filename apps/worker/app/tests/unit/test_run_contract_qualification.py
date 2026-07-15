from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.broker.contract_test_broker import QUALIFIED_TOSS_OPENAPI_SHA256
from app.application.use_cases.run_contract_qualification import (
    RunContractQualification,
)


async def test_contract_qualification_emits_complete_passing_manifest() -> None:
    started_at = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    report = await RunContractQualification().execute(
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=5),
    )

    assert report.result == "pass"
    assert report.suite_version == "contract-test-qualification-v2"
    assert report.to_json()["suite_version"] == "contract-test-qualification-v2"
    assert report.openapi_sha256 == QUALIFIED_TOSS_OPENAPI_SHA256
    assert [item.check_id for item in report.checks] == [
        "cancel_lifecycle",
        "create_lifecycle",
        "fault_injection",
        "ledger_invariants",
        "production_order_network_zero",
        "status_partial_terminal",
    ]
    assert all(item.status == "pass" for item in report.checks)
    assert all(len(item.evidence_sha256) == 64 for item in report.checks)
    ledger = next(item for item in report.checks if item.check_id == "ledger_invariants")
    assert ledger.metrics == {
        "balanced_transaction_count": 1,
        "position_quantity": 4,
        "provider_identity_change_blocked": True,
        "projection_backed_by_journal": True,
    }
    network = next(
        item for item in report.checks if item.check_id == "production_order_network_zero"
    )
    assert network.metrics["request_count"] == 0
    assert report.evidence_manifest() == report.to_json()["evidence_manifest"]


async def test_contract_qualification_is_deterministic_for_same_inputs() -> None:
    started_at = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=5)

    first = await RunContractQualification().execute(
        started_at=started_at,
        completed_at=completed_at,
    )
    second = await RunContractQualification().execute(
        started_at=started_at,
        completed_at=completed_at,
    )

    assert first.to_json() == second.to_json()


async def test_unpinned_contract_artifact_fails_without_network_evidence() -> None:
    started_at = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    report = await RunContractQualification(
        contract_artifact_sha256="0" * 64,
    ).execute(
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=5),
    )

    assert report.result == "fail"
    assert any(item.status == "fail" for item in report.checks)
    network = next(
        item for item in report.checks if item.check_id == "production_order_network_zero"
    )
    assert network.status == "pass"
    assert network.metrics["request_count"] == 0


@pytest.mark.parametrize(
    ("started_at", "completed_at"),
    [
        (
            datetime(2026, 7, 15, 1, 0),
            datetime(2026, 7, 15, 1, 1, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 15, 1, 0, tzinfo=UTC),
            datetime(2026, 7, 15, 0, 59, tzinfo=UTC),
        ),
    ],
)
async def test_contract_qualification_rejects_invalid_window(
    started_at: datetime,
    completed_at: datetime,
) -> None:
    with pytest.raises(ValueError, match="contract_qualification_window_is_invalid"):
        await RunContractQualification().execute(
            started_at=started_at,
            completed_at=completed_at,
        )
