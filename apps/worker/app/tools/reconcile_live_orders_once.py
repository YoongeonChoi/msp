from __future__ import annotations

import argparse
from dataclasses import dataclass

import anyio
import httpx

from app.adapters.broker.toss_client import TossClient
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.services.order_reconciliation_service import OrderReconciliationService
from app.config import load_settings
from app.domain.common.errors import ProviderError


@dataclass(frozen=True, slots=True)
class ReconcileLiveOrdersResult:
    updated_count: int


async def _run(limit: int) -> ReconcileLiveOrdersResult:
    settings = load_settings()
    repository = SupabaseRepository(settings)
    broker = TossClient(settings)
    service = OrderReconciliationService(broker, repository)
    try:
        updated_count = await service.reconcile_live_orders(limit=limit)
        return ReconcileLiveOrdersResult(updated_count=updated_count)
    finally:
        await broker.aclose()
        await repository.aclose()


def format_result(result: ReconcileLiveOrdersResult) -> str:
    return "\n".join(
        [
            "Live Order Reconciliation",
            f"updated={result.updated_count}",
            "FINAL=PASS",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile open live orders against Toss status.")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    try:
        result = anyio.run(_run, args.limit)
    except ValueError as exc:
        print("FINAL=FAIL")
        print(str(exc))
        raise SystemExit(1) from exc
    except ProviderError as exc:
        print("FINAL=FAIL")
        print(exc.safe_message)
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        print("FINAL=FAIL")
        print("Supabase or Toss reconciliation request failed; inspect engine_events.")
        raise SystemExit(1) from exc
    print(format_result(result))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
