# CI/CD

CI/CD exists to prevent unsafe trading changes from reaching `main`.

## Workflows

### `.github/workflows/ci.yml`

Triggers:

- `pull_request`
- `push` to `main` and `develop`

Default permissions:

```yaml
permissions:
  contents: read
```

Jobs:

- Worker
  - install `apps/worker`
  - `python -m ruff check app`
  - `python -m mypy app`
  - `python -m pytest`
- Desktop
  - `npm ci`
  - `npm run desktop:lint`
  - `npm run desktop:typecheck`
  - `npm run desktop:build`
- Migrations
  - migration filename/order check
  - required migration presence
  - RLS coverage for public tables
  - no anon/public write policy patterns
  - singleton seed keeps `enabled=false`, `mode='paper'`, `live_order_allowed=false`
  - destructive migration patterns require rollback note
- Docs
  - required docs exist
  - release/rollback docs mention live guardrails and manual deployment
- Repository safety
  - non-example `.env` files are not tracked
  - workflows do not reference production trading/API secrets
  - Render `autoDeployTrigger` stays `off`
  - workflow files do not contain Render auto-deploy commands

### `.github/workflows/security.yml`

Triggers:

- `pull_request`
- `push` to `main`
- weekly schedule

Jobs:

- CodeQL for Python and JavaScript/TypeScript
- Dependency Review on PRs
- Advisory audits:
  - `npm audit --audit-level=moderate`
  - `pip-audit`
  - `bandit`
- Gitleaks secret scan
- common secret pattern scan that prints only file paths, not matched secret text
- committed `.env` file block, allowing only `.env.example`
- Workflow policy guard

Audit jobs are advisory/non-blocking where dependency noise can block urgent safety work. Findings must still be reviewed before release.

### `.github/workflows/migration-check.yml`

Triggers only when Supabase migration/seed files or the migration workflow change.

Checks:

- sequential migration filenames
- RLS enabled for every public table created in `0001_schema.sql`
- no anon/public writes
- no destructive migration without rollback note or explicit destructive migration approval text
- singleton paper safety seed
- no committed `.env` files except `.env.example`

## Workflow Security Rules

- Use minimum default permissions: `contents: read`.
- Grant elevated permissions only at the job level, such as CodeQL `security-events: write`.
- Do not use `pull_request_target`.
- Do not print secrets.
- Do not use production secrets in PR workflows, especially from forks.
- Do not add Render auto deployment.
- Do not add workflow steps that flip `bot_settings`, `live_order_allowed`, or strategy status.
- Do not commit `.env`, `.env.local`, or provider credential files. Only `.env.example` is allowed.

## Deployment

Render deployment remains manual.

Before any deploy:

1. Set `bot_settings.enabled=false`.
2. Set `live_order_allowed=false`.
3. Verify worker heartbeat shows paused or safe state.
4. Deploy manually in Render.
5. Verify heartbeat after deploy.
6. Run paper mode first.
7. Consider live only through a separate manual release checklist.

No GitHub Actions workflow should deploy the worker automatically before live trading readiness is formally approved.
