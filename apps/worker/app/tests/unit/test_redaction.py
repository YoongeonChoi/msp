from app.infrastructure.secrets_redaction import redact_mapping
from app.logging_config import _redact_event


def test_redacts_secret_like_fields() -> None:
    result = redact_mapping({"SUPABASE_SECRET_KEY": "abcdef123456", "safe": "value"})

    assert result["SUPABASE_SECRET_KEY"] == "abcdef***"
    assert result["safe"] == "value"


def test_logging_redacts_secret_like_fields() -> None:
    result = _redact_event(
        None,
        "info",
        {
            "event": "provider_check",
            "OPENAI_API_KEY": "test-secret-token-value",
            "headers": {"authorization": "Bearer abcdef123456"},
        },
    )

    rendered = str(result)
    assert "test-secret-token-value" not in rendered
    assert "Bearer abcdef123456" not in rendered
    assert "test-s***" in rendered
