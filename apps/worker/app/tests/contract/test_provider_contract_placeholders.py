from app.adapters.ai.openai_schemas import NewsClassificationSchema


def test_openai_news_schema_rejects_extra_fields() -> None:
    payload = {
        "symbol": "005930",
        "relevance_score": 0.5,
        "sentiment": "neutral",
        "event_type": "other",
        "risk_level": "low",
        "summary_short": "요약",
        "trading_relevance": 0.5,
        "confidence": 0.5,
        "unexpected": "blocked",
    }

    try:
        NewsClassificationSchema.model_validate(payload)
    except Exception as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("schema accepted an extra field")

