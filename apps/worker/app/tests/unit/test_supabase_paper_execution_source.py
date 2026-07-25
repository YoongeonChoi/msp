from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_paper_execution_source import (
    PAPER_SOURCE_RPC_ALLOWLIST,
    SupabasePaperExecutionCommandSource,
)
from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
)
from app.application.ports.persistence_authority import (
    persistence_authority_fingerprint,
)
from app.config import Settings
from app.domain.execution_v2.models import ExecutionInvariantError, build_semantic_key


def test_paper_source_is_disabled_by_default() -> None:
    settings = Settings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )

    with pytest.raises(ExecutionInvariantError, match="worker_api_is_not_enabled"):
        SupabasePaperExecutionCommandSource(
            settings,
            account_id="paper-primary",
            release_sha="a" * 40,
        )


def test_paper_source_rpc_allowlist_is_source_only() -> None:
    assert {
        "claim_paper_execution_v1",
        "load_claimed_paper_execution_bundle_v1",
        "complete_paper_execution_source_v1",
    } == PAPER_SOURCE_RPC_ALLOWLIST


def test_paper_source_rejects_non_paper_environment_and_account_mismatch() -> None:
    contract_settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_ENVIRONMENT="contract_test",
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="contract-test-primary",
        MOCK_PROVIDERS=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )
    with pytest.raises(ExecutionInvariantError, match="requires_paper_environment"):
        SupabasePaperExecutionCommandSource(
            contract_settings,
            account_id="contract-test-primary",
            release_sha="a" * 40,
        )

    with pytest.raises(ExecutionInvariantError, match="configured_account_mismatch"):
        SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-secondary",
            release_sha="a" * 40,
        )


async def test_paper_source_exposes_only_read_only_runtime_identity() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=[])
    )
    async with httpx.AsyncClient(transport=transport) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )

        expected_authority = persistence_authority_fingerprint(
            namespace="supabase-worker-api",
            origin="https://example.supabase.co",
            profile="worker_api",
        )
        assert source.account_id == "paper-primary"
        assert source.release_sha == "a" * 40
        assert source.persistence_authority == expected_authority
        assert source.base_url == "https://example.supabase.co/rest/v1/rpc"
        assert not hasattr(source, "client")
        assert not hasattr(source, "headers")
        for field, value in (
            ("account_id", "paper-secondary"),
            ("release_sha", "b" * 40),
            ("persistence_authority", "supabase-worker-api:" + "f" * 64),
            ("base_url", "https://attacker.invalid/rest/v1/rpc"),
        ):
            with pytest.raises(AttributeError):
                setattr(source, field, value)


async def test_paper_source_rejects_non_origin_supabase_url() -> None:
    settings = _enabled_settings().model_copy(
        update={"supabase_url": "https://example.supabase.co/unexpected"}
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=[])
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ExecutionInvariantError, match="paper_source_origin_is_invalid"):
            SupabasePaperExecutionCommandSource(
                settings,
                account_id="paper-primary",
                release_sha="a" * 40,
                client=client,
            )


async def test_paper_source_owned_client_disables_environment_proxy_discovery() -> None:
    source = SupabasePaperExecutionCommandSource(
        _enabled_settings(),
        account_id="paper-primary",
        release_sha="a" * 40,
    )
    try:
        assert getattr(cast(Any, source)._client, "_trust_env", None) is False
    finally:
        await source.close()


async def test_paper_source_claims_only_the_configured_account() -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    command_id = str(uuid4())
    intent_id = str(uuid4())
    claim_token = str(uuid4())
    worker_id = str(uuid4())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": command_id,
                    "intent_id": intent_id,
                    "kind": "new_candidate",
                    "claim_token": claim_token,
                    "source_revision": 2,
                    "worker_id": worker_id,
                    "release_sha": "a" * 40,
                    "available_at": (now - timedelta(minutes=1)).isoformat(),
                    "claimed_at": now.isoformat(),
                    "claim_expires_at": (now + timedelta(seconds=30)).isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        claim = await source.claim_available_paper_execution(
            worker_id=worker_id,
            release_sha="a" * 40,
            now=now,
            lease_ttl=timedelta(seconds=30),
        )

    assert claim is not None
    assert claim.command_id == command_id
    assert seen[0].url.path == "/rest/v1/rpc/claim_paper_execution_v1"
    assert json.loads(seen[0].content) == {
        "p_account_id": "paper-primary",
        "p_worker_id": worker_id,
        "p_release_sha": "a" * 40,
        "p_now": now.isoformat(),
        "p_lease_seconds": 30,
    }


async def test_paper_source_loads_strict_bundle_and_reschedules_with_cas() -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    worker_id = str(uuid4())
    claim = ClaimedPaperExecutionCommand(
        command_id=str(uuid4()),
        intent_id=str(uuid4()),
        kind="new_candidate",
        claim_token=str(uuid4()),
        source_revision=2,
        worker_id=worker_id,
        release_sha="a" * 40,
        available_at=now - timedelta(minutes=1),
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
    next_available_at = now + timedelta(minutes=1)
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/load_claimed_paper_execution_bundle_v1"):
            return httpx.Response(200, json=[{"bundle": _bundle(claim, now)}])
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": claim.command_id,
                    "state": "pending",
                    "source_revision": 3,
                    "next_available_at": next_available_at.isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        bundle = await source.load_claimed_paper_execution_bundle(claim, now=now)
        completion = await source.complete_or_reschedule_paper_execution(
            command_id=claim.command_id,
            claim_token=claim.claim_token,
            expected_revision=claim.source_revision,
            worker_id=worker_id,
            release_sha="a" * 40,
            now=now,
            outcome="reschedule",
            next_available_at=next_available_at,
            reason_code="paper_execution_waiting_for_next_eligible_bar",
        )

    assert bundle.command.intent.id == claim.intent_id
    assert bundle.command.intent.environment == "paper"
    assert bundle.command.intent.lease_holder_id == worker_id
    assert len(bundle.command.bars) == 1
    assert bundle.command.bars[0].other_intent_filled_quantity == 0
    assert bundle.risk_input is not None
    assert bundle.risk_input.settings.live_order_allowed is False
    assert completion.state == "pending"
    assert completion.source_revision == 3
    assert seen_paths == [
        "/rest/v1/rpc/load_claimed_paper_execution_bundle_v1",
        "/rest/v1/rpc/complete_paper_execution_source_v1",
    ]


async def test_paper_source_rejects_unknown_bundle_fields() -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    claim = ClaimedPaperExecutionCommand(
        command_id=str(uuid4()),
        intent_id=str(uuid4()),
        kind="new_candidate",
        claim_token=str(uuid4()),
        source_revision=2,
        worker_id=str(uuid4()),
        release_sha="a" * 40,
        available_at=now - timedelta(minutes=1),
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
    invalid = _bundle(claim, now)
    invalid["silent_default"] = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"bundle": invalid}], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="response_fields_are_invalid"):
            await source.load_claimed_paper_execution_bundle(claim, now=now)


async def test_paper_source_requires_explicit_other_intent_bar_usage() -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    claim = ClaimedPaperExecutionCommand(
        command_id=str(uuid4()),
        intent_id=str(uuid4()),
        kind="new_candidate",
        claim_token=str(uuid4()),
        source_revision=2,
        worker_id=str(uuid4()),
        release_sha="a" * 40,
        available_at=now - timedelta(minutes=1),
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
    invalid = _bundle(claim, now)
    command = invalid["command"]
    assert isinstance(command, dict)
    bars = command["bars"]
    assert isinstance(bars, list)
    bar = bars[0]
    assert isinstance(bar, dict)
    del bar["other_intent_filled_quantity"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"bundle": invalid}], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="response_fields_are_invalid"):
            await source.load_claimed_paper_execution_bundle(claim, now=now)


async def test_paper_source_rejects_bundle_for_another_account() -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    claim = ClaimedPaperExecutionCommand(
        command_id=str(uuid4()),
        intent_id=str(uuid4()),
        kind="new_candidate",
        claim_token=str(uuid4()),
        source_revision=2,
        worker_id=str(uuid4()),
        release_sha="a" * 40,
        available_at=now - timedelta(minutes=1),
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
    invalid = _bundle(claim, now)
    command = invalid["command"]
    assert isinstance(command, dict)
    intent = command["intent"]
    assert isinstance(intent, dict)
    intent["account_id"] = "paper-secondary"
    intent["semantic_key"] = build_semantic_key(
        account_id="paper-secondary",
        environment="paper",
        strategy_version_id=str(intent["strategy_version_id"]),
        symbol=str(intent["symbol"]),
        side="buy",
        signal_valid_from=datetime.fromisoformat(str(intent["signal_valid_from"])),
        signal_valid_until=datetime.fromisoformat(str(intent["signal_valid_until"])),
        execution_policy_version=str(intent["execution_policy_version"]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"bundle": invalid}], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="bundle_account_mismatch"):
            await source.load_claimed_paper_execution_bundle(claim, now=now)


@pytest.mark.parametrize(
    ("response_state", "response_revision", "error"),
    [
        ("complete", 3, "completion_state_mismatch"),
        ("pending", 2, "completion_revision_mismatch"),
    ],
)
async def test_paper_source_requires_exact_completion_state_and_revision(
    response_state: str,
    response_revision: int,
    error: str,
) -> None:
    now = datetime(2026, 7, 15, 9, 2, tzinfo=UTC)
    command_id = str(uuid4())
    claim_token = str(uuid4())
    worker_id = str(uuid4())
    next_available_at = now + timedelta(minutes=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": command_id,
                    "state": response_state,
                    "source_revision": response_revision,
                    "next_available_at": (
                        next_available_at.isoformat()
                        if response_state == "pending"
                        else None
                    ),
                }
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match=error):
            await source.complete_or_reschedule_paper_execution(
                command_id=command_id,
                claim_token=claim_token,
                expected_revision=2,
                worker_id=worker_id,
                release_sha="a" * 40,
                now=now,
                outcome="reschedule",
                next_available_at=next_available_at,
                reason_code="paper_execution_waiting_for_next_bar",
            )


async def test_paper_source_rejects_release_mismatch_before_rpc() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabasePaperExecutionCommandSource(
            _enabled_settings(),
            account_id="paper-primary",
            release_sha="a" * 40,
            client=client,
        )
        with pytest.raises(ExecutionInvariantError, match="release_sha_mismatch"):
            await source.claim_available_paper_execution(
                worker_id=str(uuid4()),
                release_sha="b" * 40,
                now=datetime(2026, 7, 15, 9, 2, tzinfo=UTC),
                lease_ttl=timedelta(seconds=30),
            )

    assert calls == 0


def _enabled_settings() -> Settings:
    return Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )


def _bundle(
    claim: ClaimedPaperExecutionCommand,
    now: datetime,
) -> dict[str, object]:
    decision_at = now - timedelta(minutes=2)
    eligible_at = decision_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    expires_at = now + timedelta(minutes=8)
    signal_from = decision_at - timedelta(minutes=1)
    signal_until = expires_at + timedelta(minutes=1)
    strategy_id = str(uuid4())
    semantic_key = build_semantic_key(
        account_id="paper-primary",
        environment="paper",
        strategy_version_id=strategy_id,
        symbol="005930",
        side="buy",
        signal_valid_from=signal_from,
        signal_valid_until=signal_until,
        execution_policy_version="paper-policy-v1",
    )
    intent = {
        "id": claim.intent_id,
        "decision_id": str(uuid4()),
        "risk_result_id": str(uuid4()),
        "decision_feature_sha256": "1" * 64,
        "risk_allowed": True,
        "risk_reason_codes": [],
        "risk_evaluated_at": decision_at.isoformat(),
        "risk_expires_at": (now + timedelta(minutes=5)).isoformat(),
        "semantic_key": semantic_key,
        "account_id": "paper-primary",
        "environment": "paper",
        "strategy_version_id": strategy_id,
        "symbol": "005930",
        "side": "buy",
        "quantity": 1,
        "limit_price_krw": 10_000,
        "decision_at": decision_at.isoformat(),
        "signal_valid_from": signal_from.isoformat(),
        "signal_valid_until": signal_until.isoformat(),
        "execution_policy_version": "paper-policy-v1",
        "cost_schedule_version": "cost-v1",
        "cost_schedule_evidence_sha256": "2" * 64,
        "cash_commitment_krw": 10_010,
        "eligible_at": eligible_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "gate_epoch": 7,
        "lease_holder_id": claim.worker_id,
        "lease_fencing_token": 9,
        "time_in_force": "DAY",
    }
    risk_input = {
        "schema_version": 1,
        "settings": {
            "enabled": True,
            "mode": "paper",
            "live_order_allowed": False,
            "deployment_lock": False,
            "deployment_target_sha": None,
            "max_order_amount_krw": 100_000,
            "max_daily_loss_pct": 0.02,
            "max_daily_order_count": 10,
            "max_position_pct": 0.1,
            "max_sector_pct": 0.3,
            "loop_interval_sec": 30,
            "quote_freshness_sec": 60,
        },
        "signal": {
            "symbol": "005930",
            "action": "buy",
            "final_score": 0.9,
            "confidence": 0.9,
            "order_amount_krw": 100_000,
            "sector": "technology",
            "reason_json": {},
        },
        "account_state": {
            "synced": True,
            "cash_krw": 10_000_000,
            "equity_krw": 10_000_000,
            "daily_loss_pct": 0.0,
            "daily_order_count": 0,
            "synced_at": decision_at.isoformat(),
            "daily_order_count_verified": True,
        },
        "quote": {
            "symbol": "005930",
            "price_krw": 10_000,
            "as_of": decision_at.isoformat(),
            "source": "local_fixture",
        },
        "provider_health": {},
        "market_open": True,
        "existing_position_pct": 0.0,
        "sector_position_pct": 0.0,
        "available_position_quantity": 0,
        "critical_news_risk": False,
        "liquidity_ok": True,
        "volatility_ok": True,
        "cooldown_active": False,
        "duplicate_order": False,
        "strategy_version_id": strategy_id,
        "strategy_status": "active",
        "strategy_approved": True,
        "shutdown_requested": False,
    }
    return {
        "schema_version": 1,
        "command": {
            "intent": intent,
            "bars": [
                {
                    "symbol": "005930",
                    "minute": eligible_at.isoformat(),
                    "completed_at": (eligible_at + timedelta(minutes=1)).isoformat(),
                    "as_of": (eligible_at + timedelta(minutes=1)).isoformat(),
                    "source_sha256": "3" * 64,
                    "is_complete": True,
                    "open_krw": 9_900,
                    "high_krw": 10_000,
                    "low_krw": 9_800,
                    "close_krw": 9_900,
                    "volume": 100,
                    "other_intent_filled_quantity": 0,
                }
            ],
            "cost_schedule": {
                "version": "cost-v1",
                "effective_from": (decision_at - timedelta(days=1)).isoformat(),
                "effective_until": (expires_at + timedelta(days=1)).isoformat(),
                "evidence_sha256": "2" * 64,
                "settlement_days": 0,
                "settlement_evidence_sha256": "2" * 64,
                "buy_commission_rate": "0.001",
                "sell_commission_rate": "0.001",
                "sell_tax_rate": "0.002",
            },
            "execution_evidence": {
                "version": "paper-model-v1",
                "execution_policy_version": "paper-policy-v1",
                "effective_from": (decision_at - timedelta(days=1)).isoformat(),
                "effective_until": (expires_at + timedelta(days=1)).isoformat(),
                "tick_rule_version": "krx-tick-v1",
                "tick_size_krw": 1,
                "tick_rule_evidence_sha256": "4" * 64,
                "volume_source": "local_fixture",
                "volume_unit": "shares",
                "volume_evidence_sha256": "3" * 64,
                "corporate_action_status": "not_required",
                "corporate_action_evidence_sha256": "5" * 64,
                "market_calendar_version": "calendar-v1",
                "market_calendar_status": "open_sessions_verified",
                "market_calendar_evidence_sha256": "6" * 64,
                "open_session_dates": [
                    (eligible_at.date() + timedelta(days=offset)).isoformat()
                    for offset in range(4)
                ],
            },
            "position_cost_basis": None,
            "dispatch_at": eligible_at.isoformat(),
            "evaluated_at": now.isoformat(),
        },
        "risk_input": risk_input,
    }
