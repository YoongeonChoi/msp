from __future__ import annotations

import anyio

from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.config import load_settings


async def _run() -> None:
    repository = SupabaseRepository(load_settings())
    try:
        await repository.upsert_strategy_v1()
    finally:
        await repository.aclose()
    print("seeded strategy_v1_weighted_factor for paper trading")


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
