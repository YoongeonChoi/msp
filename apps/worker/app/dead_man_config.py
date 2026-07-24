from __future__ import annotations

from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.infrastructure.authenticated_webhook import (
    ReceiverAckKeyRing,
    validate_webhook_target,
)


class DeadManSettings(BaseSettings):
    """Minimal configuration for the separately deployed dead-man process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_secret_key: SecretStr = Field(alias="SUPABASE_SECRET_KEY")
    account_id: str = Field(alias="DEAD_MAN_ACCOUNT_ID")
    alert_webhook_url: SecretStr = Field(alias="DEAD_MAN_ALERT_WEBHOOK_URL")
    alert_webhook_receiver_ack_current_key_id: str = Field(
        alias="DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        exclude=True,
        repr=False,
    )
    alert_webhook_receiver_ack_current_key_b64: SecretStr = Field(
        alias="DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64"
    )
    alert_webhook_receiver_ack_previous_key_id: str | None = Field(
        default=None,
        alias="DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        exclude=True,
        repr=False,
    )
    alert_webhook_receiver_ack_previous_key_b64: SecretStr | None = Field(
        default=None,
        alias="DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    )
    forbidden_main_alert_webhook_url: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_URL",
        exclude=True,
        repr=False,
    )
    forbidden_main_receiver_ack_current_key_id: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        exclude=True,
        repr=False,
    )
    forbidden_main_receiver_ack_current_key_b64: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        exclude=True,
        repr=False,
    )
    forbidden_main_receiver_ack_previous_key_id: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        exclude=True,
        repr=False,
    )
    forbidden_main_receiver_ack_previous_key_b64: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
        exclude=True,
        repr=False,
    )
    interval_sec: int = Field(default=10, ge=5, le=300, alias="DEAD_MAN_INTERVAL_SEC")
    request_timeout_sec: float = Field(
        default=5.0,
        gt=0,
        le=30,
        alias="DEAD_MAN_REQUEST_TIMEOUT_SEC",
    )

    @field_validator(
        "alert_webhook_receiver_ack_previous_key_id",
        "alert_webhook_receiver_ack_previous_key_b64",
        "forbidden_main_alert_webhook_url",
        "forbidden_main_receiver_ack_current_key_id",
        "forbidden_main_receiver_ack_current_key_b64",
        "forbidden_main_receiver_ack_previous_key_id",
        "forbidden_main_receiver_ack_previous_key_b64",
        mode="before",
    )
    @classmethod
    def normalize_exact_empty_optional_receiver_values(cls, value: object) -> object:
        if value == "":
            return None
        if isinstance(value, SecretStr) and value.get_secret_value() == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_required_boundaries(self) -> Self:
        self._reject_main_receiver_namespace()
        if not self.supabase_url.strip():
            raise ValueError("dead_man_supabase_url_is_required")
        if not self.supabase_secret_key.get_secret_value().strip():
            raise ValueError("dead_man_supabase_secret_key_is_required")
        if not self.account_id.strip():
            raise ValueError("dead_man_account_id_is_required")
        if not self.alert_webhook_url.get_secret_value().strip():
            raise ValueError("dead_man_alert_webhook_url_is_required")
        validate_webhook_target(self.alert_webhook_url.get_secret_value())
        self.receiver_ack_key_ring()
        return self

    def _reject_main_receiver_namespace(self) -> None:
        forbidden_main_values = (
            self.forbidden_main_alert_webhook_url,
            self.forbidden_main_receiver_ack_current_key_id,
            self.forbidden_main_receiver_ack_current_key_b64,
            self.forbidden_main_receiver_ack_previous_key_id,
            self.forbidden_main_receiver_ack_previous_key_b64,
        )
        if any(value is not None for value in forbidden_main_values):
            raise ValueError("main_receiver_namespace_is_forbidden_on_dead_man")

    def receiver_ack_key_ring(self) -> ReceiverAckKeyRing:
        self._reject_main_receiver_namespace()
        return ReceiverAckKeyRing.from_base64(
            current_key_id=self.alert_webhook_receiver_ack_current_key_id,
            current_key_b64=(self.alert_webhook_receiver_ack_current_key_b64.get_secret_value()),
            previous_key_id=self.alert_webhook_receiver_ack_previous_key_id,
            previous_key_b64=(
                self.alert_webhook_receiver_ack_previous_key_b64.get_secret_value()
                if self.alert_webhook_receiver_ack_previous_key_b64 is not None
                else None
            ),
        )


def load_dead_man_settings() -> DeadManSettings:
    return DeadManSettings()  # type: ignore[call-arg]
