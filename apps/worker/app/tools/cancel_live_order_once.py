from __future__ import annotations

import argparse
from dataclasses import dataclass

import anyio
import httpx

from app.adapters.broker.toss_client import TossClient
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.services.live_order_cancellation_service import (
    LiveOrderCancellationService,
)
from app.config import load_settings
from app.domain.common.errors import KnownFailClosedError, ProviderError


@dataclass(frozen=True, slots=True)
class CancelLiveOrderResult:
    order_id: str
    status: str
    reason: str | None


async def _run(order_id: str) -> CancelLiveOrderResult:
    settings = load_settings()
    repository = SupabaseRepository(settings)
    broker = TossClient(settings)
    service = LiveOrderCancellationService(broker, repository)
    try:
        result = await service.cancel_live_order(order_id)
        return CancelLiveOrderResult(
            order_id=result.order_id,
            status=result.status,
            reason=result.reason,
        )
    finally:
        await broker.aclose()
        await repository.aclose()


def format_result(result: CancelLiveOrderResult) -> str:
    lines = [
        "Live Order Cancel",
        f"order_id={result.order_id}",
        f"status={result.status}",
    ]
    if result.reason is not None:
        lines.append(f"reason={result.reason}")
    lines.append("FINAL=PASS")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel one open live order through Toss.")
    parser.add_argument("--order-id", required=True, help="Local public.orders.id UUID to cancel")
    args = parser.parse_args()
    try:
        result = anyio.run(_run, args.order_id)
    except ValueError as exc:
        print("FINAL=FAIL")
        print(str(exc))
        raise SystemExit(1) from exc
    except (KnownFailClosedError, ProviderError) as exc:
        print("FINAL=FAIL")
        print(exc.safe_message)
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        print("FINAL=FAIL")
        print("Supabase or Toss cancel request failed; inspect engine_events.")
        raise SystemExit(1) from exc
    print(format_result(result))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
