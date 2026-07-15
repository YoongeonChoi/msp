from __future__ import annotations

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeadManSettings(BaseSettings):
    """Minimal configuration for the separately deployed dead-man process."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_secret_key: SecretStr = Field(alias="SUPABASE_SECRET_KEY")
    account_id: str = Field(alias="DEAD_MAN_ACCOUNT_ID")
    alert_webhook_url: SecretStr = Field(alias="DEAD_MAN_ALERT_WEBHOOK_URL")
    interval_sec: int = Field(default=10, ge=5, le=300, alias="DEAD_MAN_INTERVAL_SEC")
    request_timeout_sec: float = Field(
        default=5.0,
        gt=0,
        le=30,
        alias="DEAD_MAN_REQUEST_TIMEOUT_SEC",
    )

    @model_validator(mode="after")
    def validate_required_boundaries(self) -> Self:
        if not self.supabase_url.strip():
            raise ValueError("dead_man_supabase_url_is_required")
        if not self.supabase_secret_key.get_secret_value().strip():
            raise ValueError("dead_man_supabase_secret_key_is_required")
        if not self.account_id.strip():
            raise ValueError("dead_man_account_id_is_required")
        if not self.alert_webhook_url.get_secret_value().strip():
            raise ValueError("dead_man_alert_webhook_url_is_required")
        return self


def load_dead_man_settings() -> DeadManSettings:
    return DeadManSettings()  # type: ignore[call-arg]
