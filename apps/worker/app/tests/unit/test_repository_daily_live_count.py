from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.config import Settings
from app.domain.trading.entities import Order, OrderStatus, TradingMode


async def test_in_memory_repository_counts_system_live_orders_created_between() -> None:
    repository = InMemoryRepository()
    start = datetime(2026, 6, 28, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 29, 0, 0, tzinfo=UTC)
    repository.orders.extend(
        [
            _order(mode="live", status="sent", created_at=start),
            _order(mode="live", status="failed", created_at=start),
            _order(mode="live", status="blocked", created_at=start),
            _order(mode="paper", status="paper", created_at=start),
            _order(mode="live", status="sent", created_at=end),
        ]
    )

    count = await repository.count_system_live_orders_created_between(start, end)

    assert count == 2


async def test_supabase_repository_count_uses_live_non_blocked_daily_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"id": str(uuid4())}, {"id": str(uuid4())}],
            request=request,
        )

    repository = SupabaseRepository(
        Settings(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
        )
    )
    await repository.client.aclose()
    repository.client = httpx.AsyncClient(
        timeout=10.0,
        headers=repository.headers,
        transport=httpx.MockTransport(handler),
    )

    count = await repository.count_system_live_orders_created_between(
        datetime(2026, 6, 27, 15, 0, tzinfo=UTC),
        datetime(2026, 6, 28, 15, 0, tzinfo=UTC),
    )

    await repository.aclose()
    assert count == 2
    assert len(requests) == 1
    request_url = str(requests[0].url)
    assert "/rest/v1/orders?" in request_url
    assert "select=id" in request_url
    assert "mode=eq.live" in request_url
    assert "status=neq.blocked" in request_url
    assert "created_at=gte.2026-06-27T15%3A00%3A00%2B00%3A00" in request_url
    assert "created_at=lt.2026-06-28T15%3A00%3A00%2B00%3A00" in request_url


async def test_supabase_repository_strategy_version_falls_back_to_active_row() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        request_url = str(request.url)
        if "version_name=eq.strategy_v1_weighted_factor" in request_url:
            return httpx.Response(200, json=[], request=request)
        assert "status=in.(paper,active)" in request_url
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "version_name": "weighted_factor_v1_seed",
                    "status": "active",
                    "strategy_type": "WeightedFactorStrategyV1",
                    "weights": {
                        "technical": 0.35,
                        "fundamental": 0.25,
                        "market_sector": 0.15,
                        "news_event": 0.15,
                        "portfolio": 0.10,
                    },
                    "params": {"buy_threshold": 0.68, "sell_threshold": 0.25},
                }
            ],
            request=request,
        )

    repository = SupabaseRepository(
        Settings(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
        )
    )
    await repository.client.aclose()
    repository.client = httpx.AsyncClient(
        timeout=10.0,
        headers=repository.headers,
        transport=httpx.MockTransport(handler),
    )

    strategy_version = await repository.load_active_strategy_version()

    await repository.aclose()
    assert strategy_version is not None
    assert strategy_version.version == "weighted_factor_v1_seed"
    assert [str(request.url).split("/rest/v1/")[1].split("?")[0] for request in requests] == [
        "strategy_versions",
        "strategy_versions",
    ]


async def test_supabase_repository_encodes_idempotency_key_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[], request=request)

    repository = SupabaseRepository(
        Settings(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
        )
    )
    await repository.client.aclose()
    repository.client = httpx.AsyncClient(
        timeout=10.0,
        headers=repository.headers,
        transport=httpx.MockTransport(handler),
    )

    exists = await repository.idempotency_key_exists("abc&limit=100")

    await repository.aclose()
    assert exists is False
    assert len(requests) == 1
    request_url = str(requests[0].url)
    assert "idempotency_key=eq.abc%26limit%3D100" in request_url
    assert "idempotency_key=eq.abc&limit=100" not in request_url


def _order(
    *,
    mode: TradingMode,
    status: OrderStatus,
    created_at: datetime,
) -> Order:
    return Order(
        id=uuid4(),
        decision_id=uuid4(),
        symbol="005930",
        action="buy",
        mode=mode,
        status=status,
        amount_krw=100_000,
        idempotency_key=str(uuid4()),
        reason=None,
        created_at=created_at,
    )
