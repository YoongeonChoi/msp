from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    run_once: bool = Field(default=False, alias="RUN_ONCE")
    mock_providers: bool = Field(default=True, alias="MOCK_PROVIDERS")
    bot_default_mode: str = Field(default="paper", alias="BOT_DEFAULT_MODE")
    loop_interval_sec: int = Field(default=30, alias="LOOP_INTERVAL_SEC")
    heartbeat_interval_sec: int = Field(default=30, alias="HEARTBEAT_INTERVAL_SEC")
    max_concurrent_api_calls: int = Field(default=5, alias="MAX_CONCURRENT_API_CALLS")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_secret_key: SecretStr | None = Field(default=None, alias="SUPABASE_SECRET_KEY")
    toss_client_id: SecretStr | None = Field(default=None, alias="TOSS_CLIENT_ID")
    toss_client_secret: SecretStr | None = Field(default=None, alias="TOSS_CLIENT_SECRET")
    toss_account_id: SecretStr | None = Field(default=None, alias="TOSS_ACCOUNT_ID")
    opendart_api_key: SecretStr | None = Field(default=None, alias="OPENDART_API_KEY")
    krx_api_key: SecretStr | None = Field(default=None, alias="KRX_API_KEY")
    naver_client_id: SecretStr | None = Field(default=None, alias="NAVER_CLIENT_ID")
    naver_client_secret: SecretStr | None = Field(default=None, alias="NAVER_CLIENT_SECRET")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")

    def use_supabase_repository(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key and not self.mock_providers)


def load_settings() -> Settings:
    return Settings()

