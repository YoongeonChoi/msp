# Git Rules

Use Conventional Commits:

- `feat:`
- `fix:`
- `docs:`
- `refactor:`
- `test:`
- `chore:`
- `ci:`
- `security:`

Protected areas:

- `apps/worker/app/application/services/risk_service.py`
- `apps/worker/app/application/services/execution_service.py`
- `apps/worker/app/adapters/broker/`
- `supabase/migrations/`
- `render.yaml`
- `.github/workflows/`

PRs touching protected areas must include risk impact, test evidence, rollback note, and live trading behavior change yes/no.

