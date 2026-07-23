from __future__ import annotations

import re
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    run_once: bool = Field(default=False, alias="RUN_ONCE")
    mock_providers: bool = Field(default=True, alias="MOCK_PROVIDERS")
    bot_default_mode: Literal["paper"] = Field(default="paper", alias="BOT_DEFAULT_MODE")
    loop_interval_sec: int = Field(default=30, ge=5, le=3600, alias="LOOP_INTERVAL_SEC")
    heartbeat_interval_sec: int = Field(
        default=30,
        ge=5,
        le=3600,
        alias="HEARTBEAT_INTERVAL_SEC",
    )
    max_concurrent_api_calls: int = Field(
        default=5,
        ge=1,
        le=100,
        alias="MAX_CONCURRENT_API_CALLS",
    )
    use_supabase_repository_for_mock: bool = Field(
        default=False, alias="USE_SUPABASE_REPOSITORY"
    )
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_secret_key: SecretStr | None = Field(default=None, alias="SUPABASE_SECRET_KEY")
    toss_client_id: SecretStr | None = Field(default=None, alias="TOSS_CLIENT_ID")
    toss_client_secret: SecretStr | None = Field(default=None, alias="TOSS_CLIENT_SECRET")
    toss_account_id: SecretStr | None = Field(default=None, alias="TOSS_ACCOUNT_ID")
    toss_credential_scope: Literal["read_only", "order_capable", "unknown"] = Field(
        default="unknown",
        alias="TOSS_CREDENTIAL_SCOPE",
    )
    toss_order_capable_credentials: bool | None = Field(
        default=None,
        alias="TOSS_ORDER_CAPABLE_CREDENTIALS",
    )
    kr_calendar_collection_assessment_enabled: bool = Field(
        default=False,
        alias="KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED",
    )
    kr_calendar_collection_manual_execution_enabled: bool = Field(
        default=False,
        alias="KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED",
    )
    kr_calendar_collection_holder_id: str | None = Field(
        default=None,
        alias="KR_CALENDAR_COLLECTION_HOLDER_ID",
    )
    live_order_execution_enabled: bool = Field(
        default=False,
        alias="LIVE_ORDER_EXECUTION_ENABLED",
    )
    toss_order_endpoint_enabled: bool = Field(
        default=False,
        alias="TOSS_ORDER_ENDPOINT_ENABLED",
    )
    opendart_api_key: SecretStr | None = Field(default=None, alias="OPENDART_API_KEY")
    krx_api_key: SecretStr | None = Field(default=None, alias="KRX_API_KEY")
    naver_client_id: SecretStr | None = Field(default=None, alias="NAVER_CLIENT_ID")
    naver_client_secret: SecretStr | None = Field(default=None, alias="NAVER_CLIENT_SECRET")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.5", alias="OPENAI_MODEL")
    alert_webhook_url: SecretStr | None = Field(default=None, alias="ALERT_WEBHOOK_URL")
    alert_webhook_timeout_sec: float = Field(
        default=5.0,
        gt=0,
        le=60,
        alias="ALERT_WEBHOOK_TIMEOUT_SEC",
    )
    alert_drill_max_latency_ms: int = Field(
        default=2_000,
        ge=1,
        le=600_000,
        alias="ALERT_DRILL_MAX_LATENCY_MS",
    )
    live_system_order_count_scope_accepted: bool = Field(
        default=False, alias="LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED"
    )
    paper_health_db_warning_bytes: int = Field(
        default=450_000_000,
        ge=1,
        alias="PAPER_HEALTH_DB_WARNING_BYTES",
    )
    outcome_tracking_decision_limit: int = Field(
        default=500,
        ge=1,
        le=100_000,
        alias="OUTCOME_TRACKING_DECISION_LIMIT",
    )
    outcome_tracking_price_limit: int = Field(
        default=5000,
        ge=1,
        le=1_000_000,
        alias="OUTCOME_TRACKING_PRICE_LIMIT",
    )
    execution_v2_enabled: bool = Field(default=False, alias="EXECUTION_V2_ENABLED")
    execution_v2_environment: Literal["paper", "contract_test"] = Field(
        default="paper",
        alias="EXECUTION_V2_ENVIRONMENT",
    )
    execution_v2_worker_api_enabled: bool = Field(
        default=False,
        alias="EXECUTION_V2_WORKER_API_ENABLED",
    )
    execution_v2_paper_resume_input_enabled: bool = Field(
        default=False,
        alias="EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED",
    )
    execution_v2_paper_source_input_enabled: bool = Field(
        default=False,
        alias="EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED",
    )
    execution_v2_worker_id: str | None = Field(
        default=None,
        alias="EXECUTION_V2_WORKER_ID",
    )
    execution_v2_account_id: str | None = Field(
        default=None,
        alias="EXECUTION_V2_ACCOUNT_ID",
    )
    worker_lease_ttl_sec: int = Field(
        default=30,
        ge=15,
        le=300,
        alias="WORKER_LEASE_TTL_SEC",
    )
    worker_lease_renew_interval_sec: int = Field(
        default=10,
        ge=5,
        le=120,
        alias="WORKER_LEASE_RENEW_INTERVAL_SEC",
    )
    operations_command_interval_sec: int = Field(
        default=2,
        ge=1,
        le=5,
        alias="OPERATIONS_COMMAND_INTERVAL_SEC",
    )
    operations_execution_interval_sec: int = Field(
        default=5,
        ge=1,
        le=10,
        alias="OPERATIONS_EXECUTION_INTERVAL_SEC",
    )
    operations_settlement_interval_sec: int = Field(
        default=10,
        ge=1,
        le=60,
        alias="OPERATIONS_SETTLEMENT_INTERVAL_SEC",
    )
    operations_reconciliation_interval_sec: int = Field(
        default=10,
        ge=1,
        le=60,
        alias="OPERATIONS_RECONCILIATION_INTERVAL_SEC",
    )
    operations_outbox_interval_sec: int = Field(
        default=1,
        ge=1,
        le=60,
        alias="OPERATIONS_OUTBOX_INTERVAL_SEC",
    )
    operations_heartbeat_interval_sec: int = Field(
        default=5,
        ge=1,
        le=30,
        alias="OPERATIONS_HEARTBEAT_INTERVAL_SEC",
    )

    @model_validator(mode="after")
    def validate_execution_v2_boundary(self) -> Self:
        if self.live_order_execution_enabled or self.toss_order_endpoint_enabled:
            raise ValueError("production_live_order_write_is_quarantined")
        if self.toss_order_capable_credentials is True:
            raise ValueError("order_capable_toss_credentials_are_forbidden")
        toss_credentials_present = any(
            value is not None
            for value in (
                self.toss_client_id,
                self.toss_client_secret,
                self.toss_account_id,
            )
        )
        if toss_credentials_present and (
            self.toss_credential_scope != "read_only"
            or self.toss_order_capable_credentials is not False
        ):
            raise ValueError("toss_credential_scope_must_be_explicitly_read_only")
        if self.execution_v2_worker_api_enabled and not self.execution_v2_enabled:
            raise ValueError("execution_v2_worker_api_requires_execution_v2_enabled")
        if self.execution_v2_paper_resume_input_enabled and (
            not self.execution_v2_worker_api_enabled
            or self.execution_v2_environment != "paper"
        ):
            raise ValueError(
                "paper_resume_input_requires_paper_worker_api_enablement"
            )
        if self.execution_v2_paper_source_input_enabled and (
            not self.execution_v2_worker_api_enabled
            or self.execution_v2_environment != "paper"
        ):
            raise ValueError(
                "paper_source_input_requires_paper_worker_api_enablement"
            )
        if self.execution_v2_worker_api_enabled:
            try:
                worker_id = UUID(self.execution_v2_worker_id or "")
            except ValueError as exc:
                raise ValueError("execution_v2_worker_id_is_required") from exc
            if (
                str(worker_id) != self.execution_v2_worker_id
                or worker_id.version not in {1, 2, 3, 4, 5}
            ):
                raise ValueError("execution_v2_worker_id_is_invalid")
            expected_account_id = {
                "paper": "paper-primary",
                "contract_test": "contract-test-primary",
            }[self.execution_v2_environment]
            if self.execution_v2_account_id != expected_account_id:
                raise ValueError("execution_v2_account_id_is_invalid")
            if self.worker_lease_renew_interval_sec * 2 >= self.worker_lease_ttl_sec:
                raise ValueError("worker_lease_renewal_window_is_invalid")
        if self.execution_v2_enabled and self.execution_v2_environment == "contract_test":
            if self.env.strip().lower() in {"production", "prod"}:
                raise ValueError("contract_test_execution_is_forbidden_in_production")
            if not self.mock_providers:
                raise ValueError("contract_test_execution_requires_mock_providers")
        if self.kr_calendar_collection_assessment_enabled:
            self.require_kr_calendar_collection_assessment()
        if self.kr_calendar_collection_manual_execution_enabled:
            self.require_kr_calendar_collection_manual_execution()
        return self

    def require_kr_calendar_collection_assessment(self) -> None:
        if self.kr_calendar_collection_assessment_enabled is not True:
            raise ValueError("kr_calendar_collection_assessment_disabled")
        if not _nonempty_text(self.supabase_url) or not _nonempty_secret(
            self.supabase_secret_key
        ):
            raise ValueError(
                "kr_calendar_collection_assessment_requires_supabase_credentials"
            )
        if not _hosted_supabase_https_origin(self.supabase_url):
            raise ValueError(
                "kr_calendar_collection_assessment_requires_hosted_supabase_https_origin"
            )

    def require_kr_calendar_collection_manual_execution(self) -> None:
        if self.kr_calendar_collection_manual_execution_enabled is not True:
            raise ValueError("kr_calendar_collection_manual_execution_disabled")
        self.require_kr_calendar_collection_assessment()
        if self.mock_providers is not False:
            raise ValueError(
                "kr_calendar_collection_manual_execution_requires_real_providers"
            )
        if not _nonempty_secret(self.toss_client_id) or not _nonempty_secret(
            self.toss_client_secret
        ):
            raise ValueError(
                "kr_calendar_collection_manual_execution_requires_toss_credentials"
            )
        if (
            self.toss_credential_scope != "read_only"
            or self.toss_order_capable_credentials is not False
        ):
            raise ValueError(
                "kr_calendar_collection_manual_execution_requires_read_only_credentials"
            )
        if not _canonical_uuid4(self.kr_calendar_collection_holder_id):
            raise ValueError(
                "kr_calendar_collection_manual_execution_requires_uuid4_holder_id"
            )

    def use_supabase_repository(self) -> bool:
        return bool(
            self.supabase_url
            and self.supabase_secret_key
            and (not self.mock_providers or self.use_supabase_repository_for_mock)
        )


def load_settings() -> Settings:
    return Settings()


def _nonempty_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _nonempty_secret(value: object) -> bool:
    return type(value) is SecretStr and bool(value.get_secret_value().strip())


def _canonical_uuid4(value: object) -> bool:
    try:
        parsed = UUID(value) if type(value) is str else None
    except ValueError:
        return False
    return parsed is not None and parsed.version == 4 and str(parsed) == value


_SUPABASE_HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?\.supabase\.co"
)


def _hosted_supabase_https_origin(value: object) -> bool:
    if type(value) is not str or value != value.strip():
        return False
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return False
    if hostname is None:
        return False
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    return (
        parts.scheme == "https"
        and parts.netloc.lower() == expected_netloc
        and parts.username is None
        and parts.password is None
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment
        and port in {None, 443}
        and _SUPABASE_HOST_RE.fullmatch(hostname) is not None
    )
