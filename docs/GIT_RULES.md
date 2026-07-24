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

- `main`: stable, production-ready integration baseline. Active development commits are prohibited.
- `develop`: required development and integration branch and the source branch for completed `main` integrations.
- `feature/<name>`, `fix/<name>`, and `docs/<name>`: optional isolation branches created from and merged back into `develop` when explicitly needed.
- `release/<version>`: optional release preparation branch created from `develop` when needed.

## Required Development Flow

1. Verify the current `main` commit and repository state before starting work.
2. Switch to `develop` before editing. For a fresh repository, establish it from the verified `main` commit before enabling long-lived branch protection. After that bootstrap, a missing `main` or `develop` is an incident and must not be repaired by an ordinary branch-creation push.
3. Stage only the intended files, create one atomic commit per completed change, run the relevant checks, and push the commit to `origin/develop`.
4. Keep implementation, documentation, configuration, and migration work off `main` while development is active.
5. The exact current `develop` head must pass its push CI, migration, and security gates. When the complete development scope is ready, open a reviewed pull request from `develop` to `main`; the corresponding PR merge candidate must pass all required security, PR, and release gates before integration. Do not bypass these gates with a direct `main` push.
6. Preserve the long-lived branch ancestry by using a fast-forward or merge commit. Do not squash or rebase `develop` into `main`.
7. Verify that `origin/main` points to the approved integrated commit, then fast-forward `develop` to that commit and push the synchronized `origin/develop` without force.
8. Remain on `develop` after synchronization so the next coding task starts on the development branch.

Unrelated staged or working-tree changes must never be mixed into these commits. Conflicts, divergence, or unfinished Git operations block automatic integration until they are explicitly resolved.

## Long-Lived Branch Protection

The active GitHub ruleset named `protect-long-lived-branch-ancestry` is declared in
`.github/rulesets/long-lived-branch-ancestry.json` and targets exactly
`refs/heads/main` and `refs/heads/develop`.

- `creation` prevents a deleted long-lived branch from being silently recreated.
- `deletion` prevents routine removal of either protected ref.
- `non_fast_forward` prevents force-push history replacement while preserving normal
  fast-forward pushes and merge commits.
- `bypass_actors` is empty. Recovery requires an explicit, audited ruleset change;
  there is no routine user, role, team, or app bypass.

The migration history guard independently rejects an all-zero push base. A CI failure
alone cannot undo a ref update, so the active remote ruleset is the preventive control
and the guard is the fail-closed detection layer. If a protected ref is unexpectedly
missing, stop normal development, preserve the last trusted SHA and migration checksum
evidence, recover the ref through an audited administrator procedure, restore the
ruleset, and rerun all exact-SHA gates before continuing.

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
