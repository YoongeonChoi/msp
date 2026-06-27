from __future__ import annotations

import sys

import anyio

from app.adapters.ai.openai_client import OpenAIClient
from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.services.monthly_research_service import MonthlyResearchService
from app.application.use_cases.generate_monthly_candidate import GenerateMonthlyCandidate
from app.config import Settings, load_settings
from app.domain.strategy.research import AIUpgradeCandidate


class CliUsageError(Exception):
    pass


async def _run(month: str) -> AIUpgradeCandidate:
    settings = load_settings()
    repository = _build_repository(settings)
    ai = _build_ai(settings)
    service = MonthlyResearchService(repository=repository, ai=ai)
    try:
        return await GenerateMonthlyCandidate(service).execute(month)
    finally:
        if isinstance(repository, SupabaseRepository):
            await repository.aclose()
        if isinstance(ai, OpenAIClient):
            await ai.aclose()


def _build_repository(settings: Settings) -> InMemoryRepository | SupabaseRepository:
    if settings.use_supabase_repository():
        return SupabaseRepository(settings)
    return InMemoryRepository()


def _build_ai(settings: Settings) -> OpenAIMock | OpenAIClient:
    if not settings.mock_providers and settings.openai_api_key is not None:
        return OpenAIClient(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
        )
    return OpenAIMock()


def _parse_month(argv: list[str]) -> str:
    if len(argv) != 3 or argv[1] != "--month":
        raise CliUsageError(
            "usage: python -m app.tools.generate_monthly_ai_candidate --month YYYY-MM"
        )
    month = argv[2]
    if len(month) != 7 or month[4] != "-":
        raise CliUsageError("month must use YYYY-MM format")
    return month


def main() -> None:
    try:
        month = _parse_month(sys.argv)
    except CliUsageError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    candidate = anyio.run(_run, month)
    print(
        "created monthly AI candidate "
        f"name={candidate.candidate_name} "
        f"status={candidate.status} "
        f"approval_required={candidate.approval_required}"
    )


if __name__ == "__main__":
    main()
