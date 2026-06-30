# Worker

Render Background Worker로 실행되는 Python 3.12 trading engine입니다.

기본값은 항상 안전합니다.

- `BOT_DEFAULT_MODE=paper`
- `bot_settings.enabled=false`
- `live_order_allowed=false`
- mock provider 사용 가능
- UI에서 broker 주문 호출 불가

## Local

```bash
cd apps/worker
py -m pip install -e ".[dev]"
MOCK_PROVIDERS=true RUN_ONCE=true py -m app.main
py -m pytest
```

## Desktop-visible mock cycle

`MOCK_PROVIDERS=true` normally uses in-memory persistence, so the desktop
cockpit will not see heartbeat or provider health rows. For a safe local
Supabase-backed smoke test, configure `SUPABASE_URL` and `SUPABASE_SECRET_KEY`,
then run:

```bash
py -m app.tools.seed_strategy_v1
py -m app.tools.seed_watchlist_demo
MOCK_PROVIDERS=true USE_SUPABASE_REPOSITORY=true RUN_ONCE=true py -m app.main
```
