# Release Process

Versioning:

- `v0.x.y` while G1/G2 Paper and operational gates are being qualified.

Branches:

- `main`: production-ready baseline.
- `develop`: optional integration branch.
- Short-lived feature/fix/docs branches are preferred.

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
