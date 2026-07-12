import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_mock_providers_default_to_in_memory_repository() -> None:
    settings = Settings(
        MOCK_PROVIDERS=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is False


def test_mock_providers_can_write_to_supabase_when_explicitly_enabled() -> None:
    settings = Settings(
        MOCK_PROVIDERS=True,
        USE_SUPABASE_REPOSITORY=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is True


def test_real_provider_mode_uses_supabase_when_configured() -> None:
    settings = Settings(
        MOCK_PROVIDERS=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOOP_INTERVAL_SEC", 0),
        ("HEARTBEAT_INTERVAL_SEC", 3601),
        ("MAX_CONCURRENT_API_CALLS", 0),
        ("ALERT_WEBHOOK_TIMEOUT_SEC", float("nan")),
        ("ALERT_WEBHOOK_TIMEOUT_SEC", float("inf")),
        ("ALERT_DRILL_MAX_LATENCY_MS", 0),
        ("PAPER_HEALTH_DB_WARNING_BYTES", 0),
        ("OUTCOME_TRACKING_DECISION_LIMIT", 0),
        ("OUTCOME_TRACKING_PRICE_LIMIT", 0),
    ],
)
def test_runtime_settings_reject_unsafe_numeric_values(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({name: value})
