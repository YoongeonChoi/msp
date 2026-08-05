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

The post-integration `develop` update is an ancestry-only synchronization, not a new
content merge. The pre-sync `develop` commit must be the integrated commit's second
parent, the first parent must already be its ancestor, and the complete integrated tree
must equal the pre-sync `develop` tree. The migration history guard accepts only that
tree-identical shape; any different parent order, content change, or migration rewrite
remains fail closed.

## Pre-Release Checklist

- Worker tests pass.
- Worker `ruff` passes.
- Worker type check passes.
- Desktop lint/typecheck/build pass.
- Desktop Playwright E2E and Tauri/Rust check, test, and build pass.
- Windows release candidates are rebuilt as an NSIS `setup.exe`; the emitted
  SHA-256 and Authenticode status are retained with the candidate.
- Migration check passes.
- Every migration present in the PR base is byte/mode-identical in every
  candidate commit and in the merge candidate; only a new migration may be
  added. The canonical checksum inventory covers every migration.
- Raw PG17, preinstalled-pgcrypto PG17, retained-0015 `public`, retained-0015
  `extensions`, and retained-0023 migration application/invariant checks pass.
- The approved pgcrypto preflight SHA-256 and final schema/owner/OID/ACL receipt
  are bound to the exact release SHA. Hosted owner/ACL behavior has separate
  staging evidence; local PostgreSQL owner behavior is not substituted for it.
- One external single-deployment mutex covers the uninterrupted preflight,
  replay, and postflight sequence. The preflight advisory lock alone is not a
  replay lock.
- Preflight and replay use the same approved role and immutable connection
  profile with a persistent `public`-first default and no session override. The
  actual replay connection records sanitized `current_user` and
  `current_schemas(false)` values before SQL; absence or mismatch blocks release.
- Repository safety check passes: no tracked non-example `.env`, no production secrets in workflows, Render auto deploy remains off.
- Security workflow has no unresolved critical finding.
- Secret scans have no unresolved finding.
- PR template is complete.
- Protected path changes have explicit risk impact and rollback plan.
- `bot_settings.enabled=false`.
- `live_order_allowed=false`.
- Rollback target identified.

## Windows Desktop Artifact

On a Windows release host, follow [Windows Desktop](WINDOWS_DESKTOP.md) and run:

```powershell
npm ci
npx playwright install chromium
npm run desktop:bundle:windows
```

The command validates only the hosted Supabase URL and publishable key, runs the
Desktop and native gates, builds NSIS explicitly, and emits a SHA-256 file. A
successful unsigned build is a local/test artifact, not a trusted public release.
External distribution additionally requires an operator-owned Authenticode signing
identity and a successful `-RequireSignature` run. The installer never packages
Worker, broker, Supabase service-role, Render, or provider secrets.

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
