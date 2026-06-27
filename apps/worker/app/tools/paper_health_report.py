from __future__ import annotations

from collections.abc import Mapping

import anyio
import httpx

from app.adapters.persistence.paper_health_repository import SupabasePaperHealthRepository
from app.application.services.paper_health_models import PaperHealthReport, PaperHealthResult
from app.application.services.paper_health_report_service import PaperHealthReportService
from app.config import load_settings

SENSITIVE_TEXT_MARKERS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "sk-",
)


async def _run() -> PaperHealthReport:
    settings = load_settings()
    repository = SupabasePaperHealthRepository(settings)
    service = PaperHealthReportService(
        repository,
        db_warning_threshold_bytes=settings.paper_health_db_warning_bytes,
    )
    try:
        return await service.collect()
    finally:
        await repository.aclose()


def format_paper_health_report(report: PaperHealthReport) -> str:
    lines = [
        "Paper Trading Health Report",
        "",
        "[bot_settings]",
        f"enabled={report.settings.enabled}",
        f"mode={report.settings.mode}",
        f"live_order_allowed={report.settings.live_order_allowed}",
        "",
        "[worker]",
        "latest_heartbeat_age_sec=" + _optional_int(report.heartbeat_age_seconds),
        "",
        "[providers]",
    ]
    if report.providers:
        lines.extend(
            f"{provider.provider}: healthy={provider.healthy} status={provider.status}"
            for provider in report.providers
        )
    else:
        lines.append("no provider health rows")
    lines.extend(
        [
            "",
            "[last_24h_decisions_by_action]",
            _format_counts(report.decisions_by_action),
            "",
            "[last_24h_orders_by_status]",
            _format_counts(report.orders_by_status),
            "",
            "[consistency]",
            f"live_like_orders={report.live_like_order_count}",
            f"duplicate_idempotency_keys={len(report.duplicate_idempotency_keys)}",
            "decisions_missing_reason_json="
            + _optional_count(report.decisions_missing_reason_json),
            "decisions_missing_feature_json="
            + _optional_count(report.decisions_missing_feature_json),
            "orders_missing_reason_json=" + _optional_count(report.orders_missing_reason_json),
            f"orders_missing_idempotency_key={report.orders_missing_idempotency_key}",
            f"missing_optional_outcome_rows={report.missing_optional_outcome_rows}",
            "",
            "[recent_error_critical_engine_events]",
        ]
    )
    if report.recent_error_events:
        lines.extend(
            f"{event.level} {_safe_text(event.component)}: {_safe_text(event.message)}"
            for event in report.recent_error_events
        )
    else:
        lines.append("none")
    lines.extend(
        [
            "",
            "[db_size]",
            "db_size_bytes=" + _optional_int(report.db_size_bytes),
            "",
            "[findings]",
        ]
    )
    if report.findings:
        lines.extend(
            f"{finding.severity.value} {finding.code}: {finding.message}"
            for finding in report.findings
        )
    else:
        lines.append("none")
    lines.extend(["", f"FINAL={report.result.value}"])
    return "\n".join(lines)


def _format_counts(counts: Mapping[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _optional_count(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _optional_int(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _safe_text(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in SENSITIVE_TEXT_MARKERS):
        return "[redacted]"
    return value[:240]


def main() -> None:
    try:
        report = anyio.run(_run)
    except ValueError as exc:
        print("FINAL=FAIL")
        print(str(exc))
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        print("FINAL=FAIL")
        print("Supabase query failed; check worker server-side env and database access.")
        raise SystemExit(1) from exc
    print(format_paper_health_report(report))
    if report.result == PaperHealthResult.FAIL:
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
