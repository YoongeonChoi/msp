from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import assert_never

from app.application.ports.paper_health_port import PaperHealthRepositoryPort, PaperHealthRows
from app.application.services.paper_health_models import (
    EngineEventSummary,
    PaperHealthFinding,
    PaperHealthReport,
    PaperHealthResult,
    PaperHealthSeverity,
    ProviderHealthSummary,
)
from app.application.services.paper_health_row_parsing import (
    age_seconds,
    count_by,
    duplicate_idempotency_keys,
    event_summaries,
    is_market_hours,
    latest_providers,
    missing_key_count,
    missing_optional_json_count,
    missing_optional_outcomes,
    utc_now,
)
from app.domain.trading.entities import BotSettings

STALE_HEARTBEAT_SECONDS = 300
REPEATED_CRITICAL_EVENT_COUNT = 2


class PaperHealthReportService:
    def __init__(
        self,
        repository: PaperHealthRepositoryPort,
        db_warning_threshold_bytes: int = 450_000_000,
    ) -> None:
        self.repository = repository
        self.db_warning_threshold_bytes = db_warning_threshold_bytes

    async def collect(self, now: datetime | None = None) -> PaperHealthReport:
        checked_at = utc_now(now)
        since = checked_at - timedelta(hours=24)
        settings = await self.repository.load_bot_settings()
        rows = await self.repository.load_paper_health_rows(since)
        report = _build_report(settings, rows, checked_at, self.db_warning_threshold_bytes)
        await self.repository.record_engine_event(
            _event_level(report.result),
            "paper_ops",
            "paper_health_report",
            report.engine_event_details(),
        )
        return report


def _build_report(
    settings: BotSettings,
    rows: PaperHealthRows,
    now: datetime,
    db_warning_threshold_bytes: int,
) -> PaperHealthReport:
    latest_heartbeat = rows.latest_heartbeats[0] if rows.latest_heartbeats else None
    heartbeat_age = age_seconds(latest_heartbeat, "created_at", now)
    heartbeat_release_sha = _heartbeat_detail_string(latest_heartbeat, "release_sha")
    heartbeat_release_source = _heartbeat_detail_string(
        latest_heartbeat,
        "release_source",
    )
    providers = latest_providers(rows.api_health)
    decisions_by_action = count_by(rows.decisions_last_24h, "action")
    orders_by_status = count_by(rows.orders_last_24h, "status")
    duplicate_keys = duplicate_idempotency_keys(rows.order_key_rows)
    recent_events = event_summaries(rows.recent_engine_events)
    outcome_missing = missing_optional_outcomes(
        rows.orders_last_24h, rows.outcomes_last_24h, now
    )
    findings = _findings(
        settings=settings,
        heartbeat_age_seconds=heartbeat_age,
        providers=providers,
        decisions_by_action=decisions_by_action,
        orders_by_status=orders_by_status,
        live_like_order_count=len(rows.live_like_orders),
        duplicate_idempotency_keys=duplicate_keys,
        orders_missing_idempotency_key=missing_key_count(rows.order_key_rows),
        recent_events=recent_events,
        db_size_bytes=rows.db_size_bytes,
        db_warning_threshold_bytes=db_warning_threshold_bytes,
        missing_optional_outcome_rows=outcome_missing,
        now=now,
    )
    return PaperHealthReport(
        settings=settings,
        heartbeat_age_seconds=heartbeat_age,
        heartbeat_release_sha=heartbeat_release_sha,
        heartbeat_release_source=heartbeat_release_source,
        providers=providers,
        decisions_by_action=decisions_by_action,
        orders_by_status=orders_by_status,
        live_like_order_count=len(rows.live_like_orders),
        duplicate_idempotency_keys=duplicate_keys,
        decisions_missing_reason_json=missing_optional_json_count(
            rows.decisions_last_24h, ("reason_json",)
        ),
        decisions_missing_feature_json=missing_optional_json_count(
            rows.decisions_last_24h,
            ("feature_json", "feature_snapshot_json", "feature_snapshot"),
        ),
        orders_missing_reason_json=missing_optional_json_count(
            rows.orders_last_24h, ("reason_json",)
        ),
        orders_missing_idempotency_key=missing_key_count(rows.order_key_rows),
        recent_error_events=recent_events,
        db_size_bytes=rows.db_size_bytes,
        missing_optional_outcome_rows=outcome_missing,
        findings=findings,
        result=_result_from_findings(findings),
    )


def _findings(
    *,
    settings: BotSettings,
    heartbeat_age_seconds: int | None,
    providers: Sequence[ProviderHealthSummary],
    decisions_by_action: Mapping[str, int],
    orders_by_status: Mapping[str, int],
    live_like_order_count: int,
    duplicate_idempotency_keys: Sequence[str],
    orders_missing_idempotency_key: int,
    recent_events: Sequence[EngineEventSummary],
    db_size_bytes: int | None,
    db_warning_threshold_bytes: int,
    missing_optional_outcome_rows: int,
    now: datetime,
) -> tuple[PaperHealthFinding, ...]:
    findings: list[PaperHealthFinding] = []
    if settings.live_order_allowed:
        findings.append(_critical("live_order_allowed_true", "live_order_allowed is true"))
    if settings.mode == "live":
        findings.append(_critical("mode_live", "bot_settings.mode is live"))
    if live_like_order_count > 0:
        findings.append(_critical("live_like_orders_found", "live-like order status exists"))
    if duplicate_idempotency_keys:
        findings.append(_critical("duplicate_idempotency_key", "duplicate idempotency_key exists"))
    if heartbeat_age_seconds is None:
        findings.append(_critical("heartbeat_missing", "worker heartbeat is missing"))
    elif heartbeat_age_seconds > STALE_HEARTBEAT_SECONDS:
        findings.append(_critical("heartbeat_stale", "worker heartbeat is stale over 5 minutes"))
    if _operational_critical_event_count(recent_events) >= REPEATED_CRITICAL_EVENT_COUNT:
        findings.append(_critical("repeated_critical_events", "repeated critical events exist"))
    degraded = [provider.provider for provider in providers if not provider.healthy]
    if degraded:
        findings.append(
            _warning("provider_degraded", "provider health degraded: " + ", ".join(degraded))
        )
    if is_market_hours(now) and settings.enabled and sum(decisions_by_action.values()) == 0:
        findings.append(_warning("no_decisions_market_hours", "no decisions during market hours"))
    blocked_count = orders_by_status.get("blocked", 0)
    order_count = sum(orders_by_status.values())
    if blocked_count >= 5 or (order_count >= 3 and blocked_count * 2 >= order_count):
        findings.append(_warning("many_blocked_orders", "many blocked paper orders"))
    if db_size_bytes is not None and db_size_bytes > db_warning_threshold_bytes:
        findings.append(_warning("db_size_high", "database size is above warning threshold"))
    if orders_missing_idempotency_key > 0:
        findings.append(
            _warning("orders_missing_idempotency_key", "orders missing idempotency_key")
        )
    if missing_optional_outcome_rows > 0:
        findings.append(_warning("missing_optional_outcomes", "optional outcome rows are missing"))
    return tuple(findings)


def _event_level(result: PaperHealthResult) -> str:
    match result:
        case PaperHealthResult.PASS:
            return "info"
        case PaperHealthResult.WARN:
            return "warning"
        case PaperHealthResult.FAIL:
            return "critical"
        case unreachable:
            assert_never(unreachable)


def _result_from_findings(findings: Sequence[PaperHealthFinding]) -> PaperHealthResult:
    if any(finding.severity == PaperHealthSeverity.CRITICAL for finding in findings):
        return PaperHealthResult.FAIL
    if findings:
        return PaperHealthResult.WARN
    return PaperHealthResult.PASS


def _critical(code: str, message: str) -> PaperHealthFinding:
    return PaperHealthFinding(PaperHealthSeverity.CRITICAL, code, message)


def _warning(code: str, message: str) -> PaperHealthFinding:
    return PaperHealthFinding(PaperHealthSeverity.WARNING, code, message)


def _heartbeat_detail_string(
    latest_heartbeat: Mapping[str, object] | None,
    key: str,
) -> str | None:
    if latest_heartbeat is None:
        return None
    details = latest_heartbeat.get("details")
    if not isinstance(details, Mapping):
        return None
    value = details.get(key)
    if not isinstance(value, str):
        return None
    return value[:80]


def _operational_critical_event_count(events: Sequence[EngineEventSummary]) -> int:
    return sum(
        1
        for event in events
        if event.level == "critical"
        and not (
            event.component == "paper_ops"
            and event.message == "paper_health_report"
        )
    )
