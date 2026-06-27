# Worker Instructions

## Overview

Python modular monolith trading engine. Safety and fail-closed behavior outrank convenience.

## Structure

```text
app/domain/        Entities, value objects, pure policies
app/application/   Ports, services, use cases
app/adapters/      Provider and persistence implementations
app/infrastructure Cross-cutting runtime mechanisms
app/tests/         Unit, integration, contract tests
```

## Where To Look

| Task | Location |
| --- | --- |
| Trading cycle | `app/application/use_cases/run_trading_cycle.py` |
| Risk gates | `app/domain/risk/policies/`, `app/application/services/risk_service.py` |
| Execution | `app/application/services/execution_service.py` |
| Strategy scoring | `app/application/services/signal_service.py` |
| Provider ports | `app/application/ports/` |
| Provider mocks | `app/adapters/*/*_mock.py` |

## Rules

- Domain must not import `httpx`, Supabase, OpenAI, or provider clients.
- Application depends on ports and domain only.
- Only `ExecutionService` may call `BrokerPort.place_order`.
- `TossMock` must not return successful live order execution.
- Live order must be blocked unless all risk policies pass.
- Unknown provider response schema means `ProviderSchemaError` or equivalent fail-closed path.
- No blind retry for order creation.
- Every blocked order needs a persisted reason.
- OpenAI adapter may return research/classification only.

## Commands

```bash
cd apps/worker
py -m pip install -e ".[dev]"
MOCK_PROVIDERS=true RUN_ONCE=true py -m app.main
py -m pytest
py -m ruff check app
py -m mypy app
```

