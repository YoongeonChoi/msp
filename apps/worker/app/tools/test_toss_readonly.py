from __future__ import annotations

from dataclasses import dataclass

import anyio

from app.adapters.broker.toss_client import TossClient
from app.adapters.broker.toss_models import TossCandleQuery, TossOrderListQuery
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.config import Settings, load_settings
from app.domain.common.errors import ProviderError


@dataclass(frozen=True, slots=True)
class TossReadonlyCheckResult:
    masked_accounts: list[str]
    holdings_count: int
    price_count: int
    candle_count: int
    open_order_count: int


async def _run() -> TossReadonlyCheckResult:
    settings = load_settings()
    repository = _build_repository(settings)
    client: TossClient | None = None
    try:
        client = TossClient(settings)
        result = await _check_readonly(client)
        await repository.record_api_health(
            "toss",
            True,
            {
                "command": "test_toss_readonly",
                "accounts_count": len(result.masked_accounts),
                "holdings_count": result.holdings_count,
                "price_count": result.price_count,
                "candle_count": result.candle_count,
                "open_order_count": result.open_order_count,
            },
        )
        return result
    except ProviderError as exc:
        await repository.record_api_health(
            "toss",
            False,
            {"command": "test_toss_readonly", "safe_message": exc.safe_message},
        )
        raise
    finally:
        if client is not None:
            await client.aclose()
        if isinstance(repository, SupabaseRepository):
            await repository.aclose()


async def _check_readonly(client: TossClient) -> TossReadonlyCheckResult:
    accounts = await client.list_accounts()
    if not accounts:
        raise ProviderError("toss", "toss_accounts_empty")
    account_seq = accounts[0].account_seq
    holdings = await client.get_holdings(account_seq=account_seq)
    prices = await client.get_prices(["005930"])
    candles = await client.get_candles(TossCandleQuery(symbol="005930", interval="1d", count=5))
    orders = await client.list_orders(TossOrderListQuery(account_seq=account_seq))
    return TossReadonlyCheckResult(
        masked_accounts=[
            f"account_no={_mask_identifier(account.account_no)}, "
            f"account_seq={_mask_identifier(str(account.account_seq))}, "
            f"type={account.account_type}"
            for account in accounts
        ],
        holdings_count=len(holdings.items),
        price_count=len(prices),
        candle_count=len(candles.candles),
        open_order_count=len(orders.orders),
    )


def _build_repository(settings: Settings) -> InMemoryRepository | SupabaseRepository:
    if settings.use_supabase_repository():
        return SupabaseRepository(settings)
    return InMemoryRepository()


def _mask_identifier(value: str) -> str:
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def main() -> None:
    try:
        result = anyio.run(_run)
    except ProviderError as exc:
        print(f"toss read-only check failed: {exc.safe_message}")
        raise SystemExit(1) from exc
    print("toss read-only check succeeded")
    for masked_account in result.masked_accounts:
        print(masked_account)
    print(
        "summary "
        f"holdings={result.holdings_count} "
        f"prices={result.price_count} "
        f"candles={result.candle_count} "
        f"open_orders={result.open_order_count}"
    )


if __name__ == "__main__":
    main()
