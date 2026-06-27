from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.application.ports.paper_health_port import PaperHealthRows
from app.application.services.paper_health_models import PaperHealthReport, PaperHealthResult
from app.application.services.paper_health_report_service import PaperHealthReportService
from app.domain.common.json import JsonObject, json_object
from app.domain.trading.entities import BotSettings
from app.tools.paper_health_report import format_paper_health_report

NOW = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)


async def test_report_passes_on_normal_paper_data() -> None:
    repository = FakePaperHealthRepository(rows=_normal_rows())

    report = await PaperHealthReportService(repository).collect(NOW)

    assert report.result == PaperHealthResult.PASS
    assert report.live_like_order_count == 0
    assert repository.engine_events[-1]["level"] == "info"


async def test_report_fails_if_live_like_order_exists() -> None:
    rows = _normal_rows(live_like_orders=[{"id": "live-1", "status": "sent"}])
    repository = FakePaperHealthRepository(rows=rows)

    report = await PaperHealthReportService(repository).collect(NOW)

    assert report.result == PaperHealthResult.FAIL
    assert _finding_codes(report) == {"live_like_orders_found"}
    assert repository.engine_events[-1]["level"] == "critical"


async def test_report_fails_if_duplicate_idempotency_key_exists() -> None:
    rows = _normal_rows(
        order_key_rows=[
            {"id": "order-1", "idempotency_key": "same-key"},
            {"id": "order-2", "idempotency_key": "same-key"},
        ]
    )
    repository = FakePaperHealthRepository(rows=rows)

    report = await PaperHealthReportService(repository).collect(NOW)

    assert report.result == PaperHealthResult.FAIL
    assert "duplicate_idempotency_key" in _finding_codes(report)


async def test_report_warns_if_provider_degraded() -> None:
    rows = _normal_rows(api_health=[_api_health("toss", healthy=False)])
    repository = FakePaperHealthRepository(rows=rows)

    report = await PaperHealthReportService(repository).collect(NOW)

    assert report.result == PaperHealthResult.WARN
    assert _finding_codes(report) == {"provider_degraded"}
    assert repository.engine_events[-1]["level"] == "warning"


async def test_report_fails_if_heartbeat_stale() -> None:
    rows = _normal_rows(heartbeat_age=timedelta(minutes=6))
    repository = FakePaperHealthRepository(rows=rows)

    report = await PaperHealthReportService(repository).collect(NOW)

    assert report.result == PaperHealthResult.FAIL
    assert "heartbeat_stale" in _finding_codes(report)


async def test_report_output_does_not_print_secrets() -> None:
    rows = _normal_rows(
        recent_engine_events=[
            {
                "level": "error",
                "component": "token_checker",
                "message": "OPENAI_API_KEY present: OPENAI_TEST_SECRET",
                "created_at": NOW.isoformat(),
            }
        ]
    )
    repository = FakePaperHealthRepository(rows=rows)

    report = await PaperHealthReportService(repository).collect(NOW)
    rendered = format_paper_health_report(report)

    assert "OPENAI_TEST_SECRET" not in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert "[redacted]" in rendered


@dataclass(slots=True)
class FakePaperHealthRepository:
    settings: BotSettings = field(default_factory=lambda: BotSettings(enabled=True))
    rows: PaperHealthRows = field(default_factory=lambda: _normal_rows())
    engine_events: list[JsonObject] = field(default_factory=list)

    async def load_bot_settings(self) -> BotSettings:
        return self.settings

    async def load_paper_health_rows(self, since: datetime) -> PaperHealthRows:
        return self.rows

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        self.engine_events.append(
            json_object(
                {"level": level, "component": component, "message": message, "details": details}
            )
        )


def _normal_rows(
    *,
    heartbeat_age: timedelta = timedelta(seconds=60),
    api_health: list[JsonObject] | None = None,
    live_like_orders: list[JsonObject] | None = None,
    order_key_rows: list[JsonObject] | None = None,
    recent_engine_events: list[JsonObject] | None = None,
) -> PaperHealthRows:
    return PaperHealthRows(
        latest_heartbeats=[{"status": "ok", "created_at": (NOW - heartbeat_age).isoformat()}],
        api_health=api_health or [_api_health("toss"), _api_health("supabase")],
        decisions_last_24h=[
            {
                "id": "decision-1",
                "action": "buy",
                "reason_json": {"summary": "paper signal"},
                "feature_snapshot": {"final_score": 0.7},
                "created_at": NOW.isoformat(),
            }
        ],
        orders_last_24h=[
            {
                "id": "order-1",
                "status": "paper",
                "idempotency_key": "paper-key-1",
                "reason_json": {"summary": "paper order"},
                "created_at": NOW.isoformat(),
            }
        ],
        live_like_orders=live_like_orders or [],
        order_key_rows=order_key_rows
        or [{"id": "order-1", "idempotency_key": "paper-key-1"}],
        recent_engine_events=recent_engine_events or [],
        outcomes_last_24h=[],
        db_size_bytes=120_000_000,
    )


def _api_health(provider: str, healthy: bool = True) -> JsonObject:
    return {
        "provider": provider,
        "healthy": healthy,
        "status": "ok" if healthy else "error",
        "checked_at": NOW.isoformat(),
    }


def _finding_codes(report: PaperHealthReport) -> set[str]:
    return {finding.code for finding in report.findings}
