from __future__ import annotations

import json

import httpx

from app.adapters.ai.openai_schemas import (
    monthly_candidate_response_format_schema,
    parse_monthly_candidate_json,
)
from app.domain.common.errors import ProviderSchemaError, ProviderUnavailableError
from app.domain.common.json import JsonObject, json_object
from app.domain.news_intel.entities import NewsClassification
from app.domain.strategy.research import AIUpgradeCandidate

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_DEFAULT_STRUCTURED_OUTPUT_MODEL = "gpt-5.5"
_STRUCTURED_OUTPUT_MODEL_PREFIXES = (
    "gpt-5",
    "gpt-4.1",
    "gpt-4o",
)


class OpenAIClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = OPENAI_DEFAULT_STRUCTURED_OUTPUT_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        headers = {
            "authorization": f"Bearer {api_key}" if api_key else "",
            "content-type": "application/json",
        }
        self.headers = headers
        self.client = client or httpx.AsyncClient(timeout=30.0, headers=headers)

    async def provider_health(self) -> bool:
        return bool(self.api_key)

    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        raise ProviderUnavailableError("openai", "openai_structured_output_not_configured")

    async def generate_monthly_candidate(self, dataset_payload: JsonObject) -> AIUpgradeCandidate:
        if not self.api_key:
            raise ProviderUnavailableError("openai", "openai_api_key_missing")
        if not is_structured_output_model_verified(self.model):
            raise ProviderUnavailableError("openai", "openai_structured_output_model_not_verified")
        response = await self.client.post(
            OPENAI_RESPONSES_URL,
            headers=self.headers,
            json={
                "model": self.model,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You are a research assistant for a Korean paper trading system. "
                            "Never place trades, deploy strategies, or request secrets. "
                            "Return only the required structured strategy candidate."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(dataset_payload, ensure_ascii=False),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "monthly_upgrade_candidate",
                        "schema": monthly_candidate_response_format_schema(),
                        "strict": True,
                    }
                },
            },
        )
        response.raise_for_status()
        candidate_text = _extract_response_text(json_object(response.json()))
        return parse_monthly_candidate_json(candidate_text).to_domain(
            base_strategy_version_id=None
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _extract_response_text(payload: JsonObject) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    output = payload.get("output")
    if not isinstance(output, list):
        raise ProviderSchemaError("openai", "missing_response_output_text")
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict):
                text = content_item.get("text")
                if isinstance(text, str) and text:
                    return text
    raise ProviderSchemaError("openai", "missing_response_output_text")


def is_structured_output_model_verified(model: str) -> bool:
    normalized = model.strip().lower()
    return any(
        normalized == prefix
        or normalized.startswith(f"{prefix}-")
        or normalized.startswith(f"{prefix}.")
        for prefix in _STRUCTURED_OUTPUT_MODEL_PREFIXES
    )
