# MSP

MSP is a Windows-friendly first stage of a personal public-equity trading system.
It implements the safety foundation from the attached architecture before any
real broker integration:

- FastAPI Control API
- SQLite local ledger for development
- system state machine: `READ_ONLY`, `PAPER`, `SHADOW`, `ARMED`, `LIVE`, `HALTED`, `KILLED`
- fail-closed command handling with idempotency keys
- paper broker adapter
- order intent state machine
- transactional outbox
- single execution lease
- reconciliation worker
- demo market data pipeline
- deterministic research scores
- rebalance proposals with portfolio-hash approval
- small local admin dashboard

The default broker is `paper`. No real orders are sent.

## Quick Start On Windows

```powershell
.\scripts\windows\setup.ps1
.\scripts\windows\run_api.ps1
```

Open http://127.0.0.1:8000 for the dashboard or http://127.0.0.1:8000/docs for
the API docs.

In separate PowerShell windows, run workers:

```powershell
.\scripts\windows\run_execution_worker.ps1
.\scripts\windows\run_reconcile_worker.ps1
```

Seed or reset local paper cash:

```powershell
.\.venv\Scripts\python -m msp.cli seed-cash --amount 10000000 --currency KRW
```

Run tests:

```powershell
.\.venv\Scripts\python -m pytest
```

## First Safe Flow

1. Start the API and workers.
2. Use `POST /v1/commands/arm` with target mode `PAPER`.
3. Create an approved order intent with `POST /v1/order-intents`.
4. The execution worker submits it to the local paper broker.
5. The reconcile worker snapshots broker positions back into the ledger.
6. Use `POST /v1/commands/kill` to stop new orders immediately.

Every state-changing API accepts an `Idempotency-Key` header.

## Demo Data To Rebalance Flow

The local demo includes a deterministic data/research/portfolio path:

```powershell
.\.venv\Scripts\python -m msp.cli run-data-once
.\.venv\Scripts\python -m msp.cli run-research-once
.\.venv\Scripts\python -m msp.cli run-portfolio-once
```

Or use the dashboard Pipeline buttons:

```text
Data -> Research -> Rebalance -> Approve Latest
```

Approval checks the proposal `portfolio_hash` before creating approved order
intents. The execution worker then submits those intents to PaperBroker.

## Real Broker Boundary

The real Toss Securities API should only be implemented behind
`BrokerAdapter`. Keep these rules:

- broker account state is authoritative
- execution worker is physically separate from research/data workers
- timeouts become `UNKNOWN`, never blind retries
- `LIVE` mode remains disabled unless `MSP_ALLOW_LIVE_MODE=true`
- never store API keys in Git or database tables

The repository includes `TossBrokerAdapter` based on the official OpenAPI guide
and OpenAPI JSON as of this build. It is not enabled by default. Real broker
execution requires all of these gates:

- `MSP_BROKER=toss`
- `MSP_ENABLE_REAL_BROKER=true`
- `MSP_ALLOW_LIVE_MODE=true`
- `TOSSINVEST_CLIENT_ID`
- `TOSSINVEST_CLIENT_SECRET`
- `TOSSINVEST_ACCOUNT_SEQ`
- system mode `LIVE`

In all non-`LIVE` modes, the execution worker uses the local PaperBroker even if
Toss credentials exist.

Official Toss docs:

- https://developers.tossinvest.com/docs
- https://developers.tossinvest.com/llms.txt
- https://openapi.tossinvest.com/openapi-docs/latest/openapi.json

Mapped Toss endpoints:

| Purpose | Endpoint |
| --- | --- |
| Token | `POST /oauth2/token` |
| Accounts | `GET /api/v1/accounts` |
| Holdings | `GET /api/v1/holdings` |
| Create order | `POST /api/v1/orders` |
| Get order | `GET /api/v1/orders/{orderId}` |
| Cancel order | `POST /api/v1/orders/{orderId}/cancel` |
| Buying power | `GET /api/v1/buying-power` |

`clientOrderId` is populated from MSP's internal idempotency key and is capped to
the Toss maximum length. Ambiguous network, 409 processing, 429 rate-limit, and
5xx order-create outcomes are mapped to `UNKNOWN` so the system reconciles
before any retry.

## Local Project Layout

```text
src/msp/
  adapters/        broker protocols and PaperBroker
  api/             FastAPI app and local dashboard
  domain/          enums and typed broker commands
  services/        state, orders, leases, reconciliation, audit
  workers/         data, research, portfolio, execution, reconcile loops
tests/             safety-flow tests
scripts/windows/   setup and run helpers
```

## Render

`render.yaml` is included as a deployment sketch only. Before any production use,
replace SQLite with managed PostgreSQL, add a real Redis/Valkey queue, configure
secrets, and deploy the execution worker only after manual promotion.
