from app.infrastructure.secrets_redaction import redact_mapping


def test_redacts_secret_like_fields() -> None:
    result = redact_mapping({"SUPABASE_SECRET_KEY": "abcdef123456", "safe": "value"})

    assert result["SUPABASE_SECRET_KEY"] == "abcdef***"
    assert result["safe"] == "value"

