# PROJECT KNOWLEDGE BASE

Generated: 2026-06-27
Commit: 6423458
Branch: main

## Overview

`kr-auto-trading-lab` is a safety-first Korean domestic stock auto-trading control system. It uses a Python Render Background Worker modular monolith, Supabase control plane, and Tauri + React desktop cockpit.

## Structure

```text
apps/worker/      Python trading engine, ports/adapters, risk and execution gates
apps/desktop/     Tauri 2 + React + Vite management cockpit
packages/shared/  TypeScript schemas shared by UI-facing code
supabase/         SQL migrations, RLS, Realtime, seed data
docs/             Architecture, safety, API gaps, deployment, runbooks
.github/          CI, security, migration checks, ownership
render.yaml       Render Background Worker blueprint
```

## Where To Look

| Task | Location | Notes |
| --- | --- | --- |
| Live order safety | `apps/worker/app/application/services/risk_service.py` | All live gates aggregate here |
| Broker execution | `apps/worker/app/application/services/execution_service.py` | Only place allowed to call `BrokerPort.place_order` |
| Toss integration | `apps/worker/app/adapters/broker/` | Live endpoint not implemented |
| Provider uncertainty | `docs/API_GAPS.md` | Add exact verification step before implementation |
| Supabase schema/RLS | `supabase/migrations/` | No public table without RLS |
| Desktop cockpit | `apps/desktop/src/App.tsx` | Korean UI, no secrets, no broker calls |
| Deploy process | `docs/RENDER_DEPLOYMENT.md` | Disable live before deploy |
| Security model | `docs/SECURITY.md`, `docs/THREAT_MODEL.md` | Trust boundaries and mitigations |

## Code Map

| Symbol | Type | Location | Role |
| --- | --- | --- | --- |
| `RunTradingCycle` | use case | `apps/worker/app/application/use_cases/run_trading_cycle.py` | Main cycle orchestration |
| `RiskService` | service | `apps/worker/app/application/services/risk_service.py` | Fail-closed live order evaluation |
| `ExecutionService` | service | `apps/worker/app/application/services/execution_service.py` | Paper orders and live proposal sequence |
| `WeightedFactorStrategyV1` | strategy | `apps/worker/app/application/services/signal_service.py` | Explainable initial scoring |
| `InMemoryRepository` | adapter | `apps/worker/app/adapters/persistence/sql_repository.py` | Local mock persistence |
| `SupabaseRepository` | adapter | `apps/worker/app/adapters/persistence/supabase_repository.py` | Server-side PostgREST persistence |
| `TossMock` | adapter | `apps/worker/app/adapters/broker/toss_mock.py` | Refuses live orders in mock mode |
| `App` | component | `apps/desktop/src/App.tsx` | Desktop cockpit shell |

## Project Rules

- Always respond to the user in Korean unless explicitly asked otherwise.
- Keep code identifiers, filenames, commands, API names, and logs in English.
- User-facing UI text must be Korean where practical.
- Code identifiers and comments use English unless Korean domain naming is unavoidable.
- Do not invent API endpoints, params, auth flows, rate limits, or schemas.
- Do not implement fake live order execution.
- Do not bypass `RiskService`.
- Do not let UI call broker/order APIs directly.
- Do not let OpenAI output execute trades or promote live strategies.
- Do not store secrets in desktop, Git, docs, logs, seed data, or `render.yaml`.
- Any uncertainty blocks live order.

## Git Workflow

- `develop` is the required development and integration branch. Do not start code, documentation, configuration, or migration work on `main`.
- Before modifying repository files, switch to `develop`. If it does not exist, create it from the verified current `main` commit.
- Stage only the intended paths, keep each change in an atomic commit, run the relevant checks, and push completed commits to `origin/develop`.
- Keep `main` stable during active development. After the development scope is complete, open a reviewed pull request from `develop` to `main`; require the exact `develop` head to pass its push CI and migration gates and the corresponding PR merge candidate to pass all required security, PR, and release gates before integration.
- Preserve the long-lived branch ancestry when integrating: use a fast-forward or merge commit, never squash or rebase `develop` into `main`.
- If the branches diverge, a conflict exists, a Git operation is unfinished, or unrelated staged changes are present, stop and resolve the state explicitly. Do not rewrite history or force-push.
- Preserve unrelated working-tree changes across branch operations and never include them in development commits.
- After successful `main` integration, fast-forward `develop` to the integrated `main` commit, push the synchronized `develop`, and remain on `develop` for the next coding task.

## Commands

```bash
cd apps/worker && py -m pip install -e ".[dev]"
cd apps/worker && MOCK_PROVIDERS=true RUN_ONCE=true py -m app.main
cd apps/worker && py -m pytest
npm install
npm run desktop:typecheck
npm run desktop:build
```

## Notes

- `bot_settings.enabled=false`, `mode=paper`, and `live_order_allowed=false` are hard defaults.
- Render `autoDeployTrigger` remains off.
- Supabase service/secret key is worker-only.
- Realtime is limited to lightweight control/status tables.
- Supabase Free budget is treated as 500MB until current plan limits are verified.

