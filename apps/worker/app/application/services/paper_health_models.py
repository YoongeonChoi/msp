from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.trading.entities import BotSettings


class PaperHealthResult(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class PaperHealthSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class PaperHealthFinding:
    severity: PaperHealthSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ProviderHealthSummary:
    provider: str
    healthy: bool
    status: str
    checked_at: datetime | None
    detail_summary: str | None


@dataclass(frozen=True, slots=True)
class EngineEventSummary:
    level: str
    component: str
    message: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class PaperHealthReport:
    settings: BotSettings
    heartbeat_age_seconds: int | None
    heartbeat_release_sha: str | None
    heartbeat_release_source: str | None
    providers: tuple[ProviderHealthSummary, ...]
    decisions_by_action: Mapping[str, int]
    orders_by_status: Mapping[str, int]
    live_like_order_count: int
    duplicate_idempotency_keys: tuple[str, ...]
    decisions_missing_reason_json: int | None
    decisions_missing_feature_json: int | None
    orders_missing_reason_json: int | None
    orders_missing_idempotency_key: int
    recent_error_events: tuple[EngineEventSummary, ...]
    db_size_bytes: int | None
    missing_optional_outcome_rows: int
    findings: tuple[PaperHealthFinding, ...]
    result: PaperHealthResult

    def engine_event_details(self) -> dict[str, object]:
        return {
            "result": self.result.value,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "heartbeat_release_sha": self.heartbeat_release_sha,
            "heartbeat_release_source": self.heartbeat_release_source,
            "live_like_order_count": self.live_like_order_count,
            "duplicate_idempotency_key_count": len(self.duplicate_idempotency_keys),
            "orders_missing_idempotency_key": self.orders_missing_idempotency_key,
            "finding_codes": [finding.code for finding in self.findings],
        }
