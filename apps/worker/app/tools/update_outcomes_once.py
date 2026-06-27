from __future__ import annotations

import anyio
import httpx

from app.adapters.persistence.outcome_tracking_repository import (
    SupabaseOutcomeTrackingRepository,
)
from app.application.services.outcome_tracking_models import OutcomeTrackingSummary
from app.application.services.outcome_tracking_service import OutcomeTrackingService
from app.config import load_settings


async def _run() -> OutcomeTrackingSummary:
    settings = load_settings()
    repository = SupabaseOutcomeTrackingRepository(settings)
    service = OutcomeTrackingService(
        repository,
        decision_limit=settings.outcome_tracking_decision_limit,
        price_limit=settings.outcome_tracking_price_limit,
    )
    try:
        return await service.update_once()
    finally:
        await repository.aclose()


def format_summary(summary: OutcomeTrackingSummary) -> str:
    return "\n".join(
        [
            "Outcome Tracking Update",
            f"processed={summary.processed_count}",
            f"upserted={summary.upserted_count}",
            f"complete={summary.complete_count}",
            f"partial={summary.partial_count}",
            f"skipped={summary.skipped_count}",
        ]
    )


def main() -> None:
    try:
        summary = anyio.run(_run)
    except ValueError as exc:
        print("FINAL=FAIL")
        print(str(exc))
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        print("FINAL=FAIL")
        print("Supabase outcome tracking query failed; check server-side env and schema.")
        raise SystemExit(1) from exc
    print(format_summary(summary))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
