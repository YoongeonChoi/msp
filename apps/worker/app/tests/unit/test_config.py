from pydantic import SecretStr

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
