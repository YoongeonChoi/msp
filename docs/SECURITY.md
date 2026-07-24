# Security

## Release security boundary

This repository implements an internal, single-entity control system for one
proprietary-capital account. The G1+G2 release supports only `paper` and local
`contract_test` execution environments.

Production order create, cancel, modify, and order-capable credentials are
prohibited. `public.bot_settings.mode` is fixed to `paper` and
`live_order_allowed=false`. No UI, database command, deployment setting, or
network route may weaken that rule. A new external-write requirement reopens
the G0 business, regulatory, and security review.

Security principles are least privilege, defense in depth, fail closed,
explicit trust boundaries, immutable evidence, and no secret or execution
authority in the desktop.

## Identity and access

Operational users authenticate with Supabase Auth TOTP MFA and must operate at
AAL2. Authorization is assigned by UUID, not email or display name, with these
roles:

- `platform_admin`: identity and role administration; cannot approve trading
  controls.
- `operator`: Paper operations, pause, emergency stop, and change requests.
- `risk_approver`: resume, policy changes, and unknown-state resolution.
- `strategy_reviewer`: strategy promotion review.
- `auditor`: read-only evidence access.
- `release_manager`: release and provider-contract artifact registration.
- `viewer`: minimum status access.

Risk-increasing commands and access changes require a fresh TOTP challenge. The
resulting `step_up_grant` expires after five minutes and is bound to the command
hash. Requester and reviewer must be different Auth user UUIDs. Self-approval,
expired grants, stale compare-and-set versions, and AAL1 sessions fail closed.

`emergency_stop` is the only single-actor exception. One AAL2 `operator` may
atomically set `enabled=false` and increment `control_epoch`. This exception can
only reduce risk. It is never queued offline or retried automatically by the UI.

## Data API and database boundary

- `private` is the source of truth for execution, accounting, control, incident,
  outbox, access, and audit records. It is not exposed through the Data API.
- `api` contains only minimal desktop projections and authenticated user RPCs.
- `worker_api` contains the fixed Worker RPC surface and is unavailable to
  `anon` and ordinary `authenticated` users.
- Every exposed table or view has RLS and explicit `GRANT`/`REVOKE` statements.
- Functions use `SECURITY INVOKER` by default. An unavoidable
  `SECURITY DEFINER` function lives in a non-exposed schema, has
  `search_path=''`, schema-qualified objects, and a restricted `EXECUTE` grant.
- `private.audit_events` is append-only. Ordinary users and Worker credentials
  cannot update or delete audit rows.
- Legacy `public.orders`, `public.positions`, `public.manual_commands`, and
  `public.audit_logs` are `legacy_unreconciled`. They are not a V2 ledger and
  cannot be backfilled into cash, positions, or performance with invented data.

The Worker may call only the reviewed `worker_api` RPC allowlist. Intent
reservation atomically checks account, `control_epoch`, strategy/policy version,
semantic key, available cash/quantity, and fencing token. Dispatch rechecks the
epoch and fencing token. An observation, event, balanced postings, projections,
and alert enqueue commit in one transaction.

## Network and provider boundary

Real Toss connectivity is read-only. The worker startup guard rejects a
production order endpoint or order-capable credential. The local
`contract_test` simulator performs create/status/cancel qualification without
network access and must never be described as an official sandbox.

Toss authentication responses are identity-encoded and limited to 64 KiB;
read responses are identity-encoded and limited to 4 MiB. The candle envelope,
page, and item schemas reject unknown fields. Toss and candle-store JSON reject
duplicate keys at every nesting level. The Worker-only candle append RPC also
requires identity encoding and a response no larger than 64 KiB. Transport,
schema, and canonicalization failures expose only fixed safe error codes, not
provider bodies, tokens, credentials, or exception chains.

The daily-candle collection job RPCs are service-role-only and expose no table
CRUD. Private job rows use RLS, while attempt transitions are append-only. Begin
binds the exact spec, revision, attempt, and holder while atomically assigning a
fence; every later mutation also rebinds that fence. The default-disabled
`RunDailyCandleCollectionJobOnce` boundary requires durable stores with the same
non-secret service-origin/profile fingerprint, calls begin before provider I/O,
and confirms a candidate fence before its single append. The public execution
boundary also removes provider/store details and cancellation messages from
escaping exception chains.
It is not wired into a runtime, scheduler, command, Desktop, feature, strategy,
or order path. The store cannot stop out-of-band I/O because the current provider
and append ports do not carry the job fence.
After a caller records those transitions, the job state has no TTL takeover or
automatic restart for an in-flight, candidate-fenced, or unknown attempt.
Completion is allowed only after PostgreSQL rechecks the exact observation
occurrence and immutable content revision. A client-reported `inserted` flag is
telemetry, not proof of durable identity.

Provider contract artifacts record source URL, retrieval time, and SHA-256.
Unknown or mismatched contracts block execution. OpenAI output has no direct or
indirect trade execution authority and may create only reviewable research
candidates.

## Secrets and sensitive data

- `.env`, `.env.local`, and all non-example environment files remain ignored
  and untracked.
- Render owns Worker secrets. Desktop may contain only
  `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- Supabase secret/service keys and all provider secrets are Worker-only. Store
  authority fingerprints bind only a canonical origin and public adapter profile;
  credentials are never fingerprint inputs.
- Webhook secrets, account numbers, authorization headers, session tokens, and
  provider raw payloads are excluded from audit and outbox payloads.
- Receiver ACK HMAC keys are server-only canonical base64 values decoding to exactly
  32 bytes. Main Worker and dead-man current/previous key rings use separate secret
  namespaces and are never available to the Desktop.
- Logs, verifier errors, and CI scan output redact values and identify only the
  affected file or field class.

## Audit, alerts, and evidence

Each audit event includes actor, session, release, correlation, reason, and the
previous event hash. A local export is not considered complete until an external
immutable archive receipt is verified against the DB hash.

`delivery_outbox` provides at-least-once delivery with lease, retry/backoff,
dedupe, and dead-letter handling. Consumers must deduplicate. Critical events
create incidents and require human ACK within five minutes; the audit trail must
distinguish delivery, human ACK, mitigation, and two-person closure.

Webhook delivery succeeds only after an HTTPS receiver returns one unambiguous
HMAC-SHA256 ACK bound to the exact serialized URL, request and response hashes,
HTTP status, context, event identity, key ID, and timestamp. Redirects, unsigned or
duplicate headers, unknown keys, stale first-attempt ACKs, and body/identity replay
fail closed. A completion-write retry may reuse the exact cached ACK under a
still-configured key: the current key, or the previous key during rotation
overlap. Retired and unknown keys fail closed, and changing the item,
destination, URL, or payload invalidates the ACK.
This shared-key transport proof authenticates the configured receiver only. It is
not a human ACK, non-repudiation proof, or evidence that an external immutable
archive actually retained the event. Those hosted gates remain separate.

## CI and supply-chain controls

GitHub Actions use `contents: read` by default. CodeQL may receive
`security-events: write` and dependency review may receive
`pull-requests: read`. Third-party Actions are pinned to full commit SHAs.
`pull_request_target`, production secrets in PR workflows, environment dumps,
automatic deployment, and any step that enables external order execution are
prohibited.

Required checks cover Python/TypeScript lint, typecheck, tests and builds,
Tauri/Rust checks, migration application, RLS/GRANT assertions, secret scanning,
dependency review, CodeQL, and production-order network denial. Scanners report
only masked findings and never print suspected secret values.

The security workflow also verifies a deterministic lock-derived dependency
inventory for npm, the Worker production lock, the pinned Python audit-tool
closure, and Cargo. A separate unsigned CI receipt binds the canonical inventory
digest and each regular-file Git blob to the exact checked-out commit. The job
installs no project dependency package, persists no checkout credential, and
references no application/deployment secret or OIDC, deploy, or order permission.
This is a tamper/staleness control, not a standard SBOM, signature, artifact
attestation, runtime inventory, or deployment authorization. Package-manager
semantic compatibility remains enforced by the separate install/locked gates.

## Residual operational conditions

Repository implementation and local verification do not prove hosted G2. Final
approval also requires two enrolled human users, external alert delivery and ACK
evidence, immutable archive receipt, independent failure-domain monitoring,
24-hour fault soak, ten consecutive Paper/Shadow trading days, and an isolated
restore drill meeting RTO 30 minutes. A hosted database unable to prove the
required disaster-scope RPO keeps G2 closed.
