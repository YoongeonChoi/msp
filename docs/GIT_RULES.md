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

## Branches

- `main`: production-ready baseline.
- `develop`: optional integration branch.
- `feature/<name>`
- `fix/<name>`
- `docs/<name>`
- `release/<version>` when needed.

Solo trunk-based work is allowed with short-lived branches and PR review checklist discipline.

## Pull Requests

Every PR must include:

- Summary
- Risk impact
- Trading behavior changed? yes/no
- Risk engine changed? yes/no
- Execution engine changed? yes/no
- DB migration changed? yes/no
- Security impact
- Tests run
- Manual verification SQL
- Rollback plan
- Live trading safety checklist

## Protected Areas

CODEOWNERS protects:

- `apps/worker/app/**/risk*`
- `apps/worker/app/**/execution*`
- `apps/worker/**/risk*`
- `apps/worker/**/execution*`
- `apps/worker/**/broker*`
- `apps/worker/app/adapters/broker/**`
- `supabase/migrations/**`
- `render.yaml`
- `.github/workflows/**`

PRs touching protected areas must include:

- risk impact
- test evidence
- rollback note
- live trading behavior change yes/no
- manual verification SQL when database state is affected

## CI/CD Rules

- Keep workflow default permissions at `contents: read`.
- Do not use `pull_request_target` unless a security rationale is documented.
- Do not add production secrets to PR workflows.
- Do not commit non-example `.env` files.
- Do not add automatic Render deploys.
- Do not enable live trading from CI.
- Do not weaken RLS or migration checks to pass CI.
