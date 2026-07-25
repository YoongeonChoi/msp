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
| V2 execution kernel | `app/application/use_cases/run_execution_v2.py`, `app/adapters/persistence/supabase_worker_api.py` |
| Deterministic paper fills | `app/application/services/paper_execution_v2.py` |
| Local contract simulator | `app/adapters/broker/contract_test_broker.py` |
| Durable operations scheduler | `app/application/services/durable_scheduler_loop.py`, `app/application/use_cases/run_durable_scheduler.py` |
| Strategy scoring | `app/application/services/signal_service.py` |
| Provider ports | `app/application/ports/` |
| Provider mocks | `app/adapters/*/*_mock.py` |

## Rules

- Domain must not import `httpx`, Supabase, OpenAI, or provider clients.
- Application depends on ports and domain only.
- Production broker order/status/cancel network calls are hard-quarantined.
- Only the local `ContractTestBroker` may receive a V2 broker order call.
- Durable V2 writes use the reviewed `worker_api` RPC allowlist; do not add direct table CRUD.
- Every dispatch must recheck `control_epoch`, lease holder, and fencing token.
- Unknown or quarantined execution state must never be retried automatically.
- `TossMock` must not return successful live order execution.
- Live order execution remains forbidden even if legacy settings appear enabled.
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

