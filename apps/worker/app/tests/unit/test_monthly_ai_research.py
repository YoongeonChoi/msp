from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.adapters.ai.openai_client import (
    OPENAI_DEFAULT_STRUCTURED_OUTPUT_MODEL,
    OpenAIClient,
    is_structured_output_model_verified,
)
from app.adapters.ai.openai_schemas import (
    MonthlyUpgradeCandidateSchema,
    monthly_candidate_response_format_schema,
    parse_monthly_candidate_json,
)
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.services.monthly_dataset_builder import MonthlyResearchDatasetBuilder
from app.application.services.monthly_research_service import MonthlyResearchService
from app.domain.common.errors import ProviderSchemaError, ProviderUnavailableError
from app.domain.common.json import JsonObject
from app.domain.news_intel.entities import NewsClassification
from app.domain.strategy.research import (
    AIUpgradeCandidate,
    CandidateWeights,
    MonthlyResearchRows,
    MonthPeriod,
)


def test_dataset_redacts_sensitive_fields_when_building_monthly_payload() -> None:
    period = MonthPeriod.from_string("2026-05")
    rows = MonthlyResearchRows(
        base_strategy_version_id=uuid4(),
        base_strategy_version="strategy_v1_weighted_factor",
        decisions=[
            {
                "symbol": "005930",
                "action": "buy",
                "final_score": 0.72,
                "feature_snapshot": {
                    "OPENAI_API_KEY": "sk-test-secret",
                    "account_number": "1234567890",
                },
            }
        ],
        outcomes=[],
        orders=[
            {
                "symbol": "005930",
                "side": "buy",
                "status": "paper",
                "amount_krw": 100000,
                "account_id": "111122223333",
            }
        ],
        news_events=[
            {
                "symbol": "005930",
                "title": "삼성전자 실적 발표",
                "source": "naver",
                "summary_short": "실적 관련 단문 요약",
                "raw_body": "전체 기사 본문은 포함되면 안 됩니다.",
                "risk_level": "low",
            }
        ],
        features_daily=[
            {"symbol": "005930", "r_1d": 0.01, "r_5d": 0.03, "volatility_20": 0.2}
        ],
        api_health=[{"provider": "openai", "healthy": True, "authorization": "Bearer secret"}],
        backtest_runs=[
            {
                "strategy": "strategy_v1_weighted_factor",
                "total_return": 0.04,
                "assumptions": {"service_role_key": "secret-value"},
            }
        ],
    )

    dataset = MonthlyResearchDatasetBuilder().build(period, rows)
    rendered = str(dataset.payload)

    assert dataset.payload["month"] == "2026-05"
    assert "sk-test-secret" not in rendered
    assert "1234567890" not in rendered
    assert "111122223333" not in rendered
    assert "전체 기사 본문" not in rendered
    assert "실적 관련 단문 요약" in rendered
    assert "service_role_key" not in rendered
    assert "secret-value" not in rendered


def test_dataset_includes_compact_monthly_research_aggregates() -> None:
    period = MonthPeriod.from_string("2026-05")
    rows = MonthlyResearchRows(
        base_strategy_version_id=uuid4(),
        base_strategy_version="strategy_v1_weighted_factor",
        decisions=[
            {"id": "d1", "symbol": "005930", "action": "buy", "final_score": 0.72},
            {"id": "d2", "symbol": "000660", "action": "hold", "final_score": 0.42},
        ],
        outcomes=[
            {
                "decision_id": "d1",
                "symbol": "005930",
                "return_1d": 0.01,
                "return_5d": 0.05,
                "return_20d": 0.08,
                "max_drawdown_20d": -0.03,
            },
            {
                "decision_id": "d2",
                "symbol": "000660",
                "return_1d": -0.01,
                "return_5d": -0.03,
                "return_20d": -0.07,
                "max_drawdown_20d": -0.10,
            },
        ],
        orders=[
            {
                "symbol": "005930",
                "status": "paper",
                "side": "buy",
                "risk_result": {"reasons": []},
            },
            {
                "symbol": "000660",
                "status": "blocked",
                "side": "buy",
                "risk_result": {"reasons": ["stale_quote", "provider_health_bad"]},
            },
        ],
        news_events=[
            {"symbol": "005930", "sentiment": "positive", "event_type": "earnings"},
            {"symbol": "000660", "sentiment": "negative", "event_type": "macro"},
        ],
        features_daily=[
            {"symbol": "005930", "r_1d": 0.01, "r_5d": 0.03, "r_20d": 0.05},
            {"symbol": "000660", "r_1d": -0.02, "r_5d": -0.04, "r_20d": -0.06},
        ],
        api_health=[
            {"provider": "toss", "healthy": False, "status": "degraded"},
            {"provider": "openai", "healthy": True, "status": "ok"},
        ],
        backtest_runs=[
            {
                "strategy": "strategy_v1_weighted_factor",
                "period_start": "2026-01-01",
                "period_end": "2026-05-31",
                "total_return": 0.12,
                "cagr": 0.18,
                "max_drawdown": -0.06,
                "win_rate": 0.58,
                "turnover": 0.42,
                "created_at": "2026-06-01T00:00:00+00:00",
            }
        ],
    )

    dataset = MonthlyResearchDatasetBuilder().build(period, rows)
    payload = dataset.payload

    assert payload["decision_count_by_action"] == {"buy": 1, "hold": 1}
    assert payload["order_count_by_status"] == {"paper": 1, "blocked": 1}
    assert payload["risk_block_reason_counts"] == {
        "stale_quote": 1,
        "provider_health_bad": 1,
    }
    assert payload["news_sentiment_distribution"] == {"positive": 1, "negative": 1}
    assert payload["news_event_distribution"] == {"earnings": 1, "macro": 1}
    assert payload["top_winning_symbols"] == [{"symbol": "005930", "return_20d": 0.08}]
    assert payload["top_losing_symbols"] == [{"symbol": "000660", "return_20d": -0.07}]
    assert payload["backtest_summary"] == {
        "count": 1,
        "latest": {
            "strategy": "strategy_v1_weighted_factor",
            "period_start": "2026-01-01",
            "period_end": "2026-05-31",
            "total_return": 0.12,
            "cagr": 0.18,
            "max_drawdown": -0.06,
            "win_rate": 0.58,
            "turnover": 0.42,
        },
    }
    warnings = payload["data_quality_warnings"]
    assert isinstance(warnings, list)
    assert "provider_health_degraded:toss" in warnings


def test_monthly_candidate_schema_accepts_required_shape() -> None:
    candidate = MonthlyUpgradeCandidateSchema.model_validate(
        {
            "base_strategy_version": "strategy_v1_weighted_factor",
            "candidate_name": "balanced_news_risk_v2",
            "candidate_weights": {
                "technical": 0.3,
                "fundamental": 0.25,
                "market_sector": 0.15,
                "news_event": 0.2,
                "portfolio": 0.1,
            },
            "candidate_params": {
                "buy_threshold": 0.7,
                "sell_threshold": 0.25,
                "max_position_pct": 0.1,
                "news_risk_penalty": 0.05,
            },
            "rationale": "뉴스 위험을 조금 더 보수적으로 반영합니다.",
            "expected_improvement": "drawdown 완화 후보",
            "risk_notes": "과최적화 주의",
            "required_backtests": ["walk_forward", "transaction_costs"],
            "approval_required": True,
        }
    )

    assert candidate.approval_required is True
    assert candidate.candidate_weights.news_event == 0.2


def test_openai_monthly_candidate_schema_is_strict_structured_output_shape() -> None:
    schema = monthly_candidate_response_format_schema()

    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    assert "approval_required" in required
    properties = schema["properties"]
    assert isinstance(properties, dict)
    params = properties["candidate_params"]
    assert isinstance(params, dict)
    assert params["additionalProperties"] is False
    assert params["required"] == [
        "buy_threshold",
        "sell_threshold",
        "max_position_pct",
        "news_risk_penalty",
    ]


def test_invalid_openai_response_is_rejected() -> None:
    with pytest.raises(ProviderSchemaError):
        parse_monthly_candidate_json('{"candidate_name":"missing required fields"}')


def test_openai_structured_output_model_allowlist_tracks_verified_models() -> None:
    assert OPENAI_DEFAULT_STRUCTURED_OUTPUT_MODEL == "gpt-5.5"
    assert is_structured_output_model_verified("gpt-5.5")
    assert is_structured_output_model_verified("gpt-4o-mini")
    assert is_structured_output_model_verified("gpt-4o-2024-08-06")
    assert not is_structured_output_model_verified("text-embedding-3-small")


async def test_openai_client_blocks_unverified_structured_output_model_before_request() -> None:
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(500, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIClient(
        api_key="test-key",
        model="text-embedding-3-small",
        client=http_client,
    )

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await client.generate_monthly_candidate({"base_strategy_version": "strategy_v1"})

    assert exc_info.value.safe_message == "openai_structured_output_model_not_verified"
    assert requested is False
    await http_client.aclose()


async def test_monthly_candidate_is_stored_as_proposed_without_strategy_deployment() -> None:
    repository = InMemoryRepository()
    strategy_before = repository.strategy_version
    service = MonthlyResearchService(repository=repository, ai=FixedCandidateAI())

    candidate = await service.generate_candidate(MonthPeriod.from_string("2026-05"))

    assert candidate.status == "proposed"
    assert candidate.approval_required is True
    assert repository.ai_upgrade_candidates == [candidate]
    assert repository.strategy_version == strategy_before
    assert repository.orders == []


def test_generate_monthly_research_cli_accepts_month_argument() -> None:
    from app.tools.generate_monthly_research import _parse_month

    assert _parse_month(["generate_monthly_research", "--month", "2026-05"]) == "2026-05"


@dataclass(frozen=True, slots=True)
class FixedCandidateAI:
    async def provider_health(self) -> bool:
        return True

    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        return NewsClassification(
            symbol=symbol,
            relevance_score=0.5,
            sentiment="neutral",
            event_type="other",
            risk_level="low",
            summary_short=summary[:120],
            trading_relevance=0.5,
            confidence=0.5,
        )

    async def generate_monthly_candidate(self, dataset_payload: JsonObject) -> AIUpgradeCandidate:
        return AIUpgradeCandidate(
            id=uuid4(),
            base_strategy_version_id=uuid4(),
            base_strategy_version=str(dataset_payload["base_strategy_version"]),
            candidate_name="balanced_news_risk_v2",
            candidate_weights=CandidateWeights(
                technical=0.3,
                fundamental=0.25,
                market_sector=0.15,
                news_event=0.2,
                portfolio=0.1,
            ),
            candidate_params={
                "buy_threshold": 0.7,
                "sell_threshold": 0.25,
                "max_position_pct": 0.1,
                "news_risk_penalty": 0.05,
            },
            rationale="뉴스 위험을 조금 더 보수적으로 반영합니다.",
            expected_improvement="drawdown 완화 후보",
            risk_notes="과최적화 주의",
            required_backtests=["walk_forward", "transaction_costs"],
            approval_required=False,
            status="deployed",
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
