# Render Deployment

`render.yaml` defines one Python Background Worker with
`autoDeployTrigger: "off"`. Repository work and local verification do not grant
permission to create or modify a hosted Render service. Hosted staging requires
separate explicit user approval and dedicated credentials.

## Release boundary

- Start with `enabled=false`, `BOT_DEFAULT_MODE=paper`, and
  `live_order_allowed=false`.
- `EXECUTION_V2_ENVIRONMENT` may be only `paper` or local `contract_test`.
- Hosted `contract_test` is disabled; it is a local zero-network qualification
  adapter, not a provider sandbox.
- Production order create/cancel/modify, order endpoints, and order-capable
  credentials are prohibited.
- Toss credentials, when separately approved, are worker-only and read-only.
- Keep `numInstances: 1` until lease/fencing qualification passes. A warm standby
  is a later operational configuration, not horizontal dispatch.

## Manual staging deployment

1. Obtain explicit approval identifying the staging Supabase/Render projects,
   synthetic dataset, alert channel, operators, release SHA, and time window.
2. Confirm the repository and hosted control plane are Paper disabled.
3. Acquire and record one external single-deployment mutex for the staging
   project. Record the PG major version, migration-history inventory, pgcrypto
   schema, owner/version/digest OIDs, canonical migration-checksum inventory,
   and the approved preflight SHA-256. With Worker/Desktop traffic stopped,
   execute the PG17 preflight from `docs/SUPABASE_SETUP.md` immediately before
   the migration runner. The mutex must remain held through postflight; the SQL
   advisory lock does not protect replay. Abort on any warning that requires
   review; do not bypass it or use migration-history repair as remediation.
4. Apply every pending migration in `supabase/README.md` to staging with the same
   approved role and immutable connection profile used for preflight. Require a
   sanitized actual-runner `current_user`/`current_schemas(false)` start receipt,
   a persistent `public`-first default, and no client/session `search_path`
   override. If the runner cannot enforce and report this contract, stop. Retain
   the exact output, final pgcrypto schema/owner/OID/ACL receipt,
   catalog/RLS/GRANT matrix, and Supabase advisor results. Do not retain a
   connection string, token, JWT, or secret.
5. Build from the reviewed release SHA. The build writes release metadata before
   installing the hash-locked Python dependencies.
6. Deploy manually; Git push alone must not deploy.
7. Verify a fresh heartbeat reports the expected release SHA, execution
   environment, control epoch, lease holder/fencing token, and ledger checkpoint.
8. Enrol two distinct TOTP AAL2 users, validate all role negative cases, and run
   maker/checker command/ACK/postcondition tests.
9. Validate alert outbox delivery, recipient dedupe, critical human ACK,
   immutable audit receipt, dead-man monitor, and the isolated restore drill.
10. Resume Paper only through a new request approved by a different risk approver.

## Fail-closed startup

Startup must fail before the loop if configuration selects Live, exposes a
production order endpoint, declares an order-capable credential, or combines
`contract_test` with a non-local/network broker. Missing or malformed execution
policy, contract hash, release SHA, Worker RPC response, or lease evidence also
fails closed.

Read-only provider outages may keep the Worker observable, but never downgrade
the execution gate or produce a guessed value. Do not print credentials,
authorization headers, account numbers, webhook URLs, or provider raw payloads.

## Roll forward and recovery

Disable Paper and increment `control_epoch` before changing an artifact. Once V2
has a journal entry, do not destructively roll back the ledger migration. Deploy
a forward fix or isolated prior application artifact, obtain a new fencing
token, reconcile all in-flight intents, and require a new maker/checker resume.
The recovery environment always starts without an order credential.

See [Operations Runbook](RUNBOOK.md), [Rollback](ROLLBACK.md), and
[Security](SECURITY.md). Production Live has no activation procedure in this
release.
