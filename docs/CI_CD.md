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
  - `npm run desktop:test`
  - install Chromium and `npm run desktop:e2e`
  - `npm run desktop:build`
- Desktop native
  - Tauri system dependencies
  - `cargo check --locked`
  - `cargo test --locked`
  - `cargo build --locked`
- Migrations
  - migration filename/order check
  - required migration presence
  - RLS coverage for `public`/`api` tables and invoker-security API views
  - fixed `worker_api` function allowlist and no desktop/public execution grants
  - disposable PostgreSQL fresh/retained-0015 apply and invariant assertions
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
- `push` to `main` and `develop`
- weekly schedule

Jobs:

- CodeQL for Python and JavaScript/TypeScript
- Dependency Review on PRs
- Blocking security gates:
  - `npm audit --audit-level=moderate`
  - `pip-audit`
  - `bandit`
- Python audit tools and their complete dependency closure are version- and
  wheel-hash-pinned in `.github/security-tools.lock`; the audit job is pinned
  to Ubuntu 24.04 x86_64 and CPython 3.12.13
- Gitleaks secret scan
- common secret pattern scan that prints only file paths, not matched secret text
- committed `.env` file block, allowing only `.env.example`
- deterministic lock-derived dependency inventory and exact checked-out revision receipt
- Workflow policy guard

Dependency and code-audit findings fail the workflow. Third-party actions are
pinned to immutable full commit SHAs, and the workflow policy guard rejects
floating action tags, unpinned Docker actions, protected secret references, and
unsafe workflow triggers. Python audit tool installation force-reinstalls only
binary wheels with `pip --require-hashes`, including the pinned `pip` dependency,
so an unreviewed package file cannot silently replace a pinned tool or
dependency. `pip check` verifies the installed closure, and the audit-tool lock
is itself checked by `pip-audit`. Every `develop` push starts CodeQL, `npm audit`,
`pip-audit`, Bandit, secret scans, and the workflow policy guard. Dependency
Review remains a PR-only diff check. The exact current `develop` head must have
a successful security run before it can become a `main` integration candidate.

When `.github/security-tools.lock` changes, resolve both top-level tools on the
pinned Ubuntu/Python target, download wheel artifacts with
`--only-binary=:all:`, recompute every SHA-256 digest, and rerun the exact
force-reinstall, `pip check`, and both `pip-audit` gates before review.

#### Lock-derived dependency evidence

The `dependency-evidence` job runs on a fresh hosted checkout with effective
`contents: read` permission. It installs no project dependency package, persists
no checkout credential, and references no application or deployment secret. It
verifies `.github/dependency-lock-manifest.v1.json` against these normalized
UTF-8 inputs:

- root and declared workspace `package.json` files plus `package-lock.json` v3
- `apps/worker/pyproject.toml`, `requirements.txt`, and the hashed production
  `requirements.lock`
- the platform-pinned `.github/security-tools.lock`
- `apps/desktop/src-tauri/Cargo.toml` and `Cargo.lock` v4
- the generator `.github/scripts/dependency_manifest.py`

The committed inventory has stable key and component ordering, contains no
timestamp or machine path, and preserves registry, nested, workspace, and
workspace-link npm locators. Root/workspace npm dependency maps are exact-bound
to their lock descriptors. Worker declaration files must agree. Python lock
markers are parsed through an explicit safe subset without rewriting literal
content, and direct dependency markers must match the declarations. Every retained
artifact hash and crates.io checksum is recorded. A
changed normalized input makes the committed inventory stale and fails the job.
Malformed locks, noncanonical paths, symlinks or junctions, unapproved lockfile
registry or Git sources, URL credentials, unhashed Python requirements,
unsupported npm/Python version or marker forms, declaration drift, and checksum
gaps fail closed.

After the static inventory passes, the verifier requires the checked-out commit
to equal the workflow's full `${{ github.sha }}`. It also verifies that every
evidence path is a regular file in that Git tree, records each Git blob ID and
raw SHA-256, and writes a bounded unsigned receipt outside the repository. The
job publishes that receipt to `GITHUB_STEP_SUMMARY`. On `pull_request`,
`${{ github.sha }}` is the checked-out merge candidate commit; it is not claimed
to be the source branch head.

To update an intentionally changed lock set, inspect the lock and manifest
diffs, then run in the candidate checkout:

```bash
python .github/scripts/dependency_manifest.py --write
python .github/scripts/dependency_manifest.py --check
```

The result is a custom lock-derived inventory and unsigned CI receipt. It is not
a CycloneDX/SPDX SBOM, installed runtime inventory, SLSA attestation, signature,
release artifact digest, deployment provenance, or release authorization.
Python dev extras, runner/OS packages, GitHub Actions, and final Worker/Tauri
artifacts remain outside this evidence and require separate release controls.
Dependency range semantics and resolver compatibility remain the responsibility
of the existing `npm ci`, Worker production-lock contract, and Cargo `--locked`
checks; this inventory does not reimplement those package managers.

### `.github/workflows/migration-check.yml`

Triggers only when Supabase migration/seed files or the migration workflow change.

Checks:

- sequential migration filenames
- RLS/invoker security for every exposed table/view
- actual disposable PostgreSQL migration application and G1/G2 assertions
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
7. Keep Production Live unavailable in UI, DB, settings, credentials, and network.

No GitHub Actions workflow may deploy the worker or change an execution control automatically.
