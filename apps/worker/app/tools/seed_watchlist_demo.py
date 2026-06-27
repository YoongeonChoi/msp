from __future__ import annotations

import anyio

from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.config import load_settings


async def _run() -> None:
    repository = SupabaseRepository(load_settings())
    try:
        await repository.upsert_watchlist_demo()
    finally:
        await repository.aclose()
    print("seeded demo watchlist symbol 005930 for paper trading")


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
