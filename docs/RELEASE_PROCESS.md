# Release Process

Versioning:

- `v0.x.y` while G1/G2 Paper and operational gates are being qualified.

Branches:

- `main`: stable, production-ready integration baseline; no active development work is committed directly here.
- `develop`: required development and integration branch and the source of completed `main` integrations.
- Short-lived feature/fix/docs branches are optional and must branch from and merge back into `develop`.

## Branch Integration

1. Complete atomic commits and push them to `origin/develop`.
2. Open a reviewed pull request from `develop` to `main`; run the push CI and migration gates against the exact `develop` head, then run the pre-release checklist plus all required security and PR gates against the corresponding merge candidate.
3. Integrate with a fast-forward or merge commit so the long-lived branch ancestry is preserved; never squash or rebase `develop` into `main`.
4. Verify that `origin/main` points to the approved integrated commit.
5. Fast-forward `develop` to the integrated `main` commit, push the synchronized `origin/develop` without force, and remain on `develop` for subsequent work.

## Pre-Release Checklist

- Worker tests pass.
- Worker `ruff` passes.
- Worker type check passes.
- Desktop lint/typecheck/build pass.
- Desktop Playwright E2E and Tauri/Rust check, test, and build pass.
- Migration check passes.
- Fresh and retained-0015 migration application/invariant checks pass.
- Repository safety check passes: no tracked non-example `.env`, no production secrets in workflows, Render auto deploy remains off.
- Security workflow has no unresolved critical finding.
- Secret scans have no unresolved finding.
- PR template is complete.
- Protected path changes have explicit risk impact and rollback plan.
- `bot_settings.enabled=false`.
- `live_order_allowed=false`.
- Rollback target identified.

## Manual Deployment

Render deploy remains manual. GitHub Actions must not deploy automatically.

1. Disable trading:

```sql
update public.bot_settings
set enabled = false,
    live_order_allowed = false,
    updated_at = now()
where id = 'singleton';
```

2. Verify heartbeat and engine events.
3. Deploy manually in Render.
4. Verify worker heartbeat after deploy.
5. Run smoke checks.
6. Enable paper mode only.
7. Observe decisions/orders.
8. Keep `live_order_allowed=false`.

## External Order Gate

This release train ends at Paper and local `contract_test`. Production order
create/cancel/modify is prohibited, not conditionally enabled. A future external
write requirement must reopen G0 business/regulatory review and establish a new
architecture, credentials, network policy, provider environment contract, and
approval program. Passing G1/G2 does not authorize that expansion.
