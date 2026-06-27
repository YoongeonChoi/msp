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

